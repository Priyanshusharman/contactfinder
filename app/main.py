"""Local web app for searching a company and exporting team contacts."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.apollo import (
    BULK_ENRICH_SIZE,
    DEFAULT_WEBHOOK_URL,
    ApolloClient,
    ApolloError,
    chunks,
    contact_from_match,
    contact_from_search,
    csv_safe,
    sort_by_post_time,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_PATH = ROOT / ".env"

CSV_COLUMNS = [
    "name",
    "designation",
    "email",
    "phone",
    "linkedin_url",
    "company",
    "domain",
    "location",
    "seniority",
    "last_linkedin_post",
]
REQUEST_ID_RE = re.compile(r"^-?\d+$")

app = FastAPI(title="Contact Finder")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class KeyIn(BaseModel):
    api_key: str

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("API key looks too short.")
        if len(value) > 500:
            raise ValueError("API key is too long.")
        return value


class CompanySearchIn(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Enter at least 2 characters.")
        if len(value) > 200:
            raise ValueError("Company name is too long.")
        return value


class ContactSearchIn(BaseModel):
    organization_id: str
    company_name: str = ""
    domain: str = ""
    titles: list[str] = Field(min_length=1)
    contact_type: Literal["email", "phone", "both"] = "email"
    limit: int = 25
    locations: list[str] = Field(default_factory=list)

    @field_validator("organization_id")
    @classmethod
    def clean_org(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Pick a company first.")
        return value

    @field_validator("titles")
    @classmethod
    def clean_titles(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for title in value:
            text = " ".join(str(title).split())
            key = text.lower()
            if not text or key in seen:
                continue
            if len(text) > 80:
                raise ValueError("A job title is too long.")
            seen.add(key)
            cleaned.append(text)
        if not cleaned:
            raise ValueError("Pick a team or enter a job title.")
        if len(cleaned) > 30:
            raise ValueError("Use 30 job titles or fewer.")
        return cleaned

    @field_validator("locations")
    @classmethod
    def clean_locations(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for location in value:
            text = " ".join(str(location).split())
            key = text.lower()
            if not text or key in {"all", "*"} or key in seen:
                continue
            if len(text) > 80:
                raise ValueError("A location name is too long.")
            seen.add(key)
            cleaned.append(text)
        if len(cleaned) > 80:
            raise ValueError("Use 80 locations or fewer.")
        return cleaned

    @field_validator("limit")
    @classmethod
    def clamp_limit(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Ask for at least 1 person.")
        if value > 100:
            raise ValueError("The limit is 100 people per search.")
        return value


class ContactRow(BaseModel):
    name: str = ""
    designation: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    company: str = ""
    domain: str = ""
    location: str = ""
    seniority: str = ""
    last_linkedin_post: str = ""


class CsvIn(BaseModel):
    contacts: list[ContactRow]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(SETTINGS_PATH)


def get_api_key() -> str:
    saved = str(read_settings().get("apollo_api_key") or "").strip()
    if saved:
        return saved
    return os.environ.get("APOLLO_API_KEY", "").strip()


def get_webhook_url() -> str:
    return os.environ.get("APOLLO_WEBHOOK_URL", "").strip() or DEFAULT_WEBHOOK_URL


def require_client() -> ApolloClient:
    api_key = get_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="Add your Apollo API key first.")
    return ApolloClient(api_key, get_webhook_url())


def enrich_detail(person_id: str, person: dict[str, Any], company: dict[str, str]) -> dict[str, str]:
    detail = {"id": person_id}
    first_name = str(person.get("first_name") or "").strip()
    if first_name and "*" not in first_name:
        detail["first_name"] = first_name
    if company.get("name"):
        detail["organization_name"] = company["name"]
    if company.get("domain"):
        detail["domain"] = company["domain"]
    return detail


def credit_amount(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def remember_phone_request(enriched: dict[str, Any], reveal_phone: bool, request_ids: list[str]) -> None:
    request_id = enriched.get("request_id")
    if reveal_phone and request_id is not None:
        request_ids.append(str(request_id))


def apply_matches(
    requested_ids: list[str],
    enriched: dict[str, Any],
    contacts: dict[str, dict[str, str]],
    company: dict[str, str],
) -> list[str]:
    matches = enriched.get("matches")
    if not isinstance(matches, list):
        people = enriched.get("people")
        matches = people if isinstance(people, list) else []
    requested = set(requested_ids)
    by_id: dict[str, dict[str, Any]] = {}
    for match in matches:
        if isinstance(match, dict) and str(match.get("id") or "") in requested:
            by_id[str(match["id"])] = match
    missed: list[str] = []
    for index, requested_id in enumerate(requested_ids):
        match = by_id.get(requested_id)
        if match is None and index < len(matches) and isinstance(matches[index], dict):
            candidate = matches[index]
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id == requested_id:
                match = candidate
        if match is None and len(requested_ids) == 1:
            match = next((item for item in matches if isinstance(item, dict)), None)
        if not isinstance(match, dict):
            missed.append(requested_id)
            continue
        contact = contact_from_match(match, company)
        contact["id"] = requested_id
        contacts[requested_id] = contact
    return missed


def key_hint(api_key: str) -> str:
    if len(api_key) < 4:
        return "saved"
    return api_key[-4:]


load_dotenv(ENV_PATH)


@app.exception_handler(ApolloError)
async def apollo_error_handler(_request, exc: ApolloError) -> JSONResponse:
    status = exc.status if 400 <= exc.status < 600 else 502
    return JSONResponse(status_code=status, content={"detail": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    parts: list[str] = []
    for error in exc.errors():
        message = str(error.get("msg", "Invalid value"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        parts.append(message)
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "Check the form and try again."})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/key")
def read_key() -> dict[str, Any]:
    api_key = get_api_key()
    return {"configured": bool(api_key), "hint": key_hint(api_key) if api_key else ""}


@app.put("/api/key")
def save_key(payload: KeyIn) -> dict[str, Any]:
    settings = read_settings()
    settings["apollo_api_key"] = payload.api_key
    write_settings(settings)
    return {"configured": True, "hint": key_hint(payload.api_key)}


@app.delete("/api/key")
def delete_key() -> dict[str, bool]:
    settings = read_settings()
    settings.pop("apollo_api_key", None)
    write_settings(settings)
    return {"configured": False}


@app.post("/api/companies")
async def search_companies(payload: CompanySearchIn) -> dict[str, Any]:
    companies = await require_client().search_companies(payload.name)
    return {"companies": companies}


@app.post("/api/contacts")
async def search_contacts(payload: ContactSearchIn) -> dict[str, Any]:
    client = require_client()
    company = {
        "name": payload.company_name.strip(),
        "domain": payload.domain.strip(),
    }
    reveal_email = payload.contact_type in {"email", "both"}
    reveal_phone = payload.contact_type in {"phone", "both"}
    people, broadened = await client.search_people(
        payload.organization_id,
        payload.titles,
        payload.limit,
        payload.locations,
        prefer_email=reveal_email,
    )
    people_by_id = {str(person["id"]): person for person in people}
    ordered_ids = list(people_by_id)
    contacts = {
        person_id: contact_from_search(person, company)
        for person_id, person in people_by_id.items()
    }
    credits = 0.0
    phone_request_ids: list[str] = []
    warnings: list[str] = []
    if broadened:
        warnings.append("Apollo had no verified emails for that filter, so this search includes a wider set of people.")

    for batch_ids in chunks(ordered_ids, BULK_ENRICH_SIZE):
        details = [enrich_detail(person_id, people_by_id[person_id], company) for person_id in batch_ids]
        try:
            enriched = await client.enrich_batch(details, reveal_email, reveal_phone)
        except ApolloError as exc:
            if any(row["email"] or row["linkedin_url"] for row in contacts.values()):
                warnings.append(exc.message)
                break
            raise
        credits += credit_amount(enriched.get("credits_consumed"))
        remember_phone_request(enriched, reveal_phone, phone_request_ids)
        missed = apply_matches(batch_ids, enriched, contacts, company)
        for person_id in missed:
            try:
                single = await client.enrich_person(enrich_detail(person_id, people_by_id[person_id], company), reveal_email, reveal_phone)
            except ApolloError as exc:
                warnings.append(exc.message)
                break
            credits += credit_amount(single.get("credits_consumed"))
            remember_phone_request(single, reveal_phone, phone_request_ids)
            apply_matches([person_id], single, contacts, company)

    rows = sort_by_post_time([contacts[person_id] for person_id in ordered_ids if person_id in contacts])
    if rows and reveal_email and not any(row["email"] for row in rows):
        warnings.append("Apollo returned these people without an email. Check that your plan still has credits, or Apollo may not have an email on file.")
    dated = sum(1 for row in rows if row["last_linkedin_post"])
    if rows and not dated:
        warnings.append("Apollo does not include last LinkedIn post times, so these people stay in search order.")
    elif dated:
        warnings.append("Sorted with the most recent LinkedIn post first. People without a post time are listed after them.")
    return {
        "contacts": rows,
        "credits_consumed": int(round(credits)),
        "phone_request_ids": phone_request_ids,
        "warning": " ".join(warnings),
    }


@app.get("/api/phones/{request_id}")
async def poll_phones(request_id: str) -> dict[str, Any]:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=400, detail="That phone lookup id is not valid.")
    result = await require_client().poll_phones(request_id)
    return result


@app.post("/api/contacts/csv")
def export_csv(payload: CsvIn) -> Response:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for contact in payload.contacts:
        row = {column: csv_safe(getattr(contact, column)) for column in CSV_COLUMNS}
        writer.writerow(row)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )
