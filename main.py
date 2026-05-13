"""
Digital Flyer Generation System - FastAPI Backend
CORRECTED VERSION with Flutterwave API verification, charge.failed handling,
and transaction_id tracking.
"""

import os
import secrets
import re
import asyncio
import httpx  # For async HTTP requests to Flutterwave API
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, status, BackgroundTasks, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlmodel import SQLModel, Field as SQLField, create_engine, Session, select, Column, Text, delete
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy.orm import sessionmaker


# =============================================================================
# CONFIGURATION
# =============================================================================

class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./flyers.db")
    FLW_SECRET_KEY: str = os.getenv("FLW_SECRET_KEY", "")
    # CRITICAL FIX #1: No hardcoded fallback - MUST be set via environment
    FLW_WEBHOOK_HASH: str = os.getenv("FLW_WEBHOOK_HASH", "")
    ADMIN_USERNAME: str = "admin_nacos"
    ADMIN_PASSWORD: str = "nacos_secure_2024"
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    PORT: int = int(os.getenv("PORT", "8000"))
    AUTO_DELETE_PENDING_HOURS: int = int(os.getenv("AUTO_DELETE_PENDING_HOURS", "24"))

    # Validate critical secrets at startup
    @classmethod
    def validate(cls):
        if not cls.FLW_WEBHOOK_HASH:
            raise RuntimeError("FLW_WEBHOOK_HASH must be set in environment variables")
        if not cls.FLW_SECRET_KEY:
            raise RuntimeError("FLW_SECRET_KEY must be set in environment variables")


settings = Settings()


# =============================================================================
# DATABASE MODEL - FULL YEARBOOK SCHEMA (FIXED with transaction_id)
# =============================================================================

class FlyerRecord(SQLModel, table=True):
    __tablename__ = "flyer_records"

    id: Optional[int] = SQLField(default=None, primary_key=True)

    # 1. CORE IDENTITY
    full_name: str = SQLField(max_length=18)
    student_portrait: Optional[str] = SQLField(sa_column=Column(Text, nullable=True), default=None)

    # 2. PERSONAL & SOCIAL DETAILS
    nickname: str = SQLField(default="")
    state_of_origin: str = SQLField(default="")
    birthday_month: str = SQLField(default="")
    birthday_day: str = SQLField(default="")
    relationship_status: str = SQLField(default="")
    hobby: str = SQLField(default="")
    social_handle: str = SQLField(default="")
    favorite_word_quote: str = SQLField(max_length=20, default="")
    class_crush: str = SQLField(default="")

    # 3. ACADEMIC PROFILE
    current_level: str = SQLField(default="")
    best_level: str = SQLField(default="")
    difficult_level: str = SQLField(default="")
    best_course: str = SQLField(default="")
    worst_course: str = SQLField(default="")
    favorite_lecturer: str = SQLField(default="")
    post_held: str = SQLField(default="")
    career_alternative: str = SQLField(default="")

    # 4. FUTURE & PROFESSIONAL
    business_skill: str = SQLField(default="")
    whats_next: str = SQLField(default="")
    best_campus_experience: str = SQLField(sa_column=Column(Text), default="")

    # PAYMENT & SYSTEM (FIXED: Added transaction_id)
    tx_ref: str = SQLField(unique=True, index=True)
    transaction_id: Optional[int] = SQLField(default=None, index=True)  # Flutterwave transaction ID
    payment_status: str = SQLField(default="pending")
    amount: float = SQLField(default=500.0)
    currency: str = SQLField(default="NGN")
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = SQLField(default=None)


# NEW: Webhook event log for idempotency and audit trail
class WebhookLog(SQLModel, table=True):
    __tablename__ = "webhook_logs"

    id: Optional[int] = SQLField(default=None, primary_key=True)
    flutterwave_event_id: int = SQLField(index=True)  # The unique 'id' from Flutterwave webhook
    event_type: str = SQLField()
    tx_ref: str = SQLField(index=True)
    transaction_id: Optional[int] = SQLField()
    status: str = SQLField()
    payload_summary: str = SQLField(sa_column=Column(Text), default="")
    processed_at: datetime = SQLField(default_factory=lambda: datetime.now(timezone.utc))


# =============================================================================
# PYDANTIC REQUEST/RESPONSE MODELS
# =============================================================================

class FlyerInitiateRequest(BaseModel):
    model_config = ConfigDict(str_max_length=500)

    full_name: str = Field(..., max_length=18)
    student_portrait: Optional[str] = Field(default=None, description="Base64 encoded photo - optional")
    nickname: str = Field(default="")
    state_of_origin: str = Field(default="")
    birthday_month: str = Field(default="")
    birthday_day: str = Field(default="")
    relationship_status: str = Field(default="Single")
    hobby: str = Field(default="")
    social_handle: str = Field(default="")
    favorite_word_quote: str = Field(default="", max_length=20)
    class_crush: str = Field(default="")
    current_level: Literal["", "ND2", "HND2 - SWD", "HND2 - NCC"] = Field(default="")
    best_level: str = Field(default="")
    difficult_level: str = Field(default="")
    best_course: str = Field(default="")
    worst_course: str = Field(default="")
    favorite_lecturer: str = Field(default="")
    post_held: str = Field(default="")
    career_alternative: str = Field(default="")
    business_skill: str = Field(default="")
    whats_next: str = Field(default="")
    best_campus_experience: str = Field(default="")

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v):
        v = re.sub(r"\s+", " ", v).strip()
        if len(v) > 18:
            raise ValueError("Full name must be max 17 chars + 1 space")
        if v.count(" ") != 1:
            raise ValueError("Must contain exactly one space (FirstName LastName)")
        return v

    @field_validator("student_portrait", mode="before")
    @classmethod
    def allow_unlimited_portrait(cls, v):
        return v


class FlyerInitiateResponse(BaseModel):
    success: bool
    message: str
    tx_ref: str
    payment_link: Optional[str] = None


class FlyerStatusResponse(BaseModel):
    tx_ref: str
    payment_status: Literal["pending", "successful", "failed"]
    full_name: str
    amount: float
    created_at: datetime


class AdminFlyerDetail(BaseModel):
    full_name: str
    student_portrait: Optional[str]
    nickname: str
    state_of_origin: str
    birthday: str
    relationship_status: str
    hobby: str
    social_handle: str
    favorite_word_quote: str
    class_crush: str
    current_level: str
    best_level: str
    difficult_level: str
    best_course: str
    worst_course: str
    favorite_lecturer: str
    post_held: str
    career_alternative: str
    business_skill: str
    whats_next: str
    best_campus_experience: str
    tx_ref: str
    transaction_id: Optional[int]
    payment_status: str
    amount: float
    created_at: str


class AdminDashboardResponse(BaseModel):
    total_records: int
    total_revenue: float
    successful_payments: int
    pending_payments: int
    failed_payments: int
    nd2_count: int
    hnd2_swd_count: int
    hnd2_ncc_count: int
    single_count: int
    married_count: int
    other_relationship_count: int
    flyers: List[AdminFlyerDetail]


class DeleteResponse(BaseModel):
    success: bool
    message: str
    deleted_count: int


class AdminUploadRequest(BaseModel):
    model_config = ConfigDict(str_max_length=500)
    full_name: str = Field(..., max_length=18)
    student_portrait: Optional[str] = Field(default=None)
    nickname: str = Field(default="")
    state_of_origin: str = Field(default="")
    birthday_month: str = Field(default="")
    birthday_day: str = Field(default="")
    relationship_status: str = Field(default="Single")
    hobby: str = Field(default="")
    social_handle: str = Field(default="")
    favorite_word_quote: str = Field(default="", max_length=20)
    class_crush: str = Field(default="")
    current_level: Literal["", "ND2", "HND2 - SWD", "HND2 - NCC"] = Field(default="")
    best_level: str = Field(default="")
    difficult_level: str = Field(default="")
    best_course: str = Field(default="")
    worst_course: str = Field(default="")
    favorite_lecturer: str = Field(default="")
    post_held: str = Field(default="")
    career_alternative: str = Field(default="")
    business_skill: str = Field(default="")
    whats_next: str = Field(default="")
    best_campus_experience: str = Field(default="")

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v):
        v = re.sub(r"\s+", " ", v).strip()
        if len(v) > 18:
            raise ValueError("Full name must be max 17 chars + 1 space")
        if v.count(" ") != 1:
            raise ValueError("Must contain exactly one space (FirstName LastName)")
        return v

    @field_validator("student_portrait", mode="before")
    @classmethod
    def allow_unlimited_portrait(cls, v):
        return v


class AdminUploadResponse(BaseModel):
    success: bool
    message: str
    tx_ref: str
    record: AdminFlyerDetail


# =============================================================================
# DATABASE SETUP
# =============================================================================

engine: AsyncEngine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)
async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all, checkfirst=True)


async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# =============================================================================
# SECURITY
# =============================================================================

security = HTTPBasic(auto_error=False)


async def verify_admin_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> bool:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    is_valid_username = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    is_valid_password = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)

    if not (is_valid_username and is_valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return True


def verify_webhook_signature(verif_hash: Optional[str]) -> bool:
    if not verif_hash:
        return False
    return secrets.compare_digest(verif_hash, settings.FLW_WEBHOOK_HASH)


# =============================================================================
# FLUTTERWAVE API VERIFICATION (CRITICAL FIX #2)
# =============================================================================

async def verify_transaction_with_flutterwave(transaction_id: int) -> dict:
    """
    Verify a transaction with Flutterwave API.
    Returns the verification response or raises HTTPException on failure.
    """
    if not settings.FLW_SECRET_KEY:
        raise HTTPException(status_code=500, detail="FLW_SECRET_KEY not configured")

    url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify"
    headers = {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                raise HTTPException(
                    status_code=400, 
                    detail=f"Flutterwave verification failed: {data.get('message', 'Unknown error')}"
                )

            return data.get("data", {})

        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Flutterwave API error: {e.response.status_code}"
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=503,
                detail=f"Could not reach Flutterwave API: {str(e)}"
            )


# =============================================================================
# UTILITIES
# =============================================================================

def generate_tx_ref() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random_part = secrets.token_hex(4).upper()
    return f"FLY-{timestamp}-{random_part}"


def generate_payment_link(tx_ref: str) -> str:
    return f"https://checkout.flutterwave.com/v3/hosted-pay?tx_ref={tx_ref}"


# =============================================================================
# AUTO-DELETE PENDING RECORDS
# =============================================================================

async def auto_delete_pending_records():
    while True:
        try:
            async with async_session_maker() as session:
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=settings.AUTO_DELETE_PENDING_HOURS)

                statement = select(FlyerRecord).where(
                    FlyerRecord.payment_status == "pending",
                    FlyerRecord.created_at < cutoff_time
                )
                result = await session.execute(statement)
                old_pending = result.scalars().all()

                deleted_count = 0
                for record in old_pending:
                    await session.delete(record)
                    deleted_count += 1

                if deleted_count > 0:
                    await session.commit()
                    print(f"[AUTO-DELETE] Deleted {deleted_count} pending records older than {settings.AUTO_DELETE_PENDING_HOURS}h")
        except Exception as e:
            print(f"[AUTO-DELETE] Error: {e}")

        await asyncio.sleep(3600)


# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()  # Validate secrets at startup
    await init_db()
    asyncio.create_task(auto_delete_pending_records())
    yield
    await engine.dispose()


app = FastAPI(
    title="NACOS Digital Flyer API",
    description="Yearbook flyer generation system for MAPOLY students",
    version="3.1.0",  # Bumped for security fixes
    lifespan=lifespan
)

# FIXED CORS: Restrict to specific origins in production
allowed_origins = [settings.FRONTEND_URL]
if settings.FRONTEND_URL == "http://localhost:3000":
    allowed_origins.append("http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["*"]
)


# =============================================================================
# ENDPOINTS
# =============================================================================

@app.post(
    "/api/flyers/initiate",
    response_model=FlyerInitiateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Flyer Generation"]
)
async def initiate_flyer(
    request: FlyerInitiateRequest,
    session: AsyncSession = Depends(get_session)
):
    """Submit complete yearbook data and initiate payment."""
    try:
        tx_ref = generate_tx_ref()

        flyer = FlyerRecord(
            full_name=request.full_name,
            student_portrait=request.student_portrait,
            nickname=request.nickname,
            state_of_origin=request.state_of_origin,
            birthday_month=request.birthday_month,
            birthday_day=request.birthday_day,
            relationship_status=request.relationship_status,
            hobby=request.hobby,
            social_handle=request.social_handle,
            favorite_word_quote=request.favorite_word_quote,
            class_crush=request.class_crush,
            current_level=request.current_level,
            best_level=request.best_level,
            difficult_level=request.difficult_level,
            best_course=request.best_course,
            worst_course=request.worst_course,
            favorite_lecturer=request.favorite_lecturer,
            post_held=request.post_held,
            career_alternative=request.career_alternative,
            business_skill=request.business_skill,
            whats_next=request.whats_next,
            best_campus_experience=request.best_campus_experience,
            tx_ref=tx_ref,
            transaction_id=None,
            payment_status="pending",
            amount=500.0,
            currency="NGN",
            created_at=datetime.now(timezone.utc)
        )

        session.add(flyer)
        await session.commit()
        await session.refresh(flyer)

        return FlyerInitiateResponse(
            success=True,
            message="Yearbook data saved. Proceed to payment.",
            tx_ref=tx_ref,
            payment_link=generate_payment_link(tx_ref)
        )

    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save flyer data: {str(e)}"
        )


# =============================================================================
# FIXED WEBHOOK ENDPOINT
# =============================================================================

@app.post(
    "/api/webhook/flutterwave",
    status_code=status.HTTP_200_OK,
    tags=["Payment"]
)
async def flutterwave_webhook(
    payload: dict,
    verif_hash: Optional[str] = Header(None, alias="verif-hash"),
    session: AsyncSession = Depends(get_session)
):
    """
    Handle Flutterwave payment webhooks with full verification.

    CRITICAL FIXES APPLIED:
    1. No debug logging of secrets
    2. API verification before updating payment status
    3. Idempotency check using webhook event ID
    4. Handles both charge.completed and charge.failed
    5. Stores transaction_id for audit trail
    """

    # Verify webhook signature (no debug logging of secrets)
    if not verify_webhook_signature(verif_hash):
        # Log security event without exposing secrets
        print(f"[WEBHOOK] Invalid signature received at {datetime.now(timezone.utc).isoformat()}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        event = payload.get("event", "")
        data = payload.get("data", {})

        # Extract Flutterwave's unique event ID for idempotency
        flutterwave_event_id = data.get("id")
        tx_ref = data.get("tx_ref")
        transaction_id = data.get("id")  # This is the Flutterwave transaction ID

        # Log webhook receipt (without sensitive data)
        print(f"[WEBHOOK] Event: {event}, tx_ref: {tx_ref}, event_id: {flutterwave_event_id}")

        # Validate required fields
        if not tx_ref:
            raise HTTPException(status_code=400, detail="Missing transaction reference")

        if not flutterwave_event_id:
            raise HTTPException(status_code=400, detail="Missing Flutterwave event ID")

        # IDEMPOTENCY CHECK: Have we processed this webhook before?
        existing_log = await session.execute(
            select(WebhookLog).where(WebhookLog.flutterwave_event_id == flutterwave_event_id)
        )
        if existing_log.scalar_one_or_none():
            print(f"[WEBHOOK] Duplicate event {flutterwave_event_id} - already processed")
            return {"status": "already_processed", "message": "Webhook already handled"}

        # Find the flyer record
        statement = select(FlyerRecord).where(FlyerRecord.tx_ref == tx_ref)
        result = await session.execute(statement)
        flyer = result.scalar_one_or_none()

        if not flyer:
            raise HTTPException(status_code=404, detail=f"Record {tx_ref} not found")

        # CRITICAL FIX: If already successful, don't reprocess (double protection)
        if flyer.payment_status == "successful":
            print(f"[WEBHOOK] Record {tx_ref} already marked successful - skipping")
            return {"status": "already_successful", "message": "Payment already confirmed"}

        # Handle different event types
        if event == "charge.completed":
            payment_status = data.get("status", "").lower()

            if payment_status == "successful":
                # CRITICAL FIX #2: Verify with Flutterwave API before giving value
                try:
                    verification_data = await verify_transaction_with_flutterwave(transaction_id)

                    # Validate amount and currency match
                    verified_amount = verification_data.get("amount")
                    verified_currency = verification_data.get("currency")
                    verified_status = verification_data.get("status", "").lower()
                    verified_tx_ref = verification_data.get("tx_ref")

                    # Security checks
                    if verified_status != "successful":
                        raise HTTPException(status_code=400, detail="Transaction not successful in Flutterwave")

                    if verified_tx_ref != tx_ref:
                        raise HTTPException(status_code=400, detail="Transaction reference mismatch")

                    if verified_amount != flyer.amount:
                        raise HTTPException(status_code=400, detail=f"Amount mismatch: expected {flyer.amount}, got {verified_amount}")

                    if verified_currency != flyer.currency:
                        raise HTTPException(status_code=400, detail=f"Currency mismatch: expected {flyer.currency}, got {verified_currency}")

                    # All checks passed - update record
                    flyer.payment_status = "successful"
                    flyer.transaction_id = transaction_id
                    flyer.updated_at = datetime.now(timezone.utc)

                    # Log the webhook
                    webhook_log = WebhookLog(
                        flutterwave_event_id=flutterwave_event_id,
                        event_type=event,
                        tx_ref=tx_ref,
                        transaction_id=transaction_id,
                        status="successful",
                        payload_summary=f"Amount: {verified_amount}, Currency: {verified_currency}"
                    )
                    session.add(webhook_log)
                    await session.commit()

                    print(f"[WEBHOOK] Payment verified and confirmed for {tx_ref}")
                    return {"status": "success", "message": "Payment verified and confirmed", "tx_ref": tx_ref}

                except HTTPException:
                    raise
                except Exception as e:
                    await session.rollback()
                    print(f"[WEBHOOK] Verification failed for {tx_ref}: {str(e)}")
                    raise HTTPException(status_code=502, detail="Payment verification failed")

            else:
                # Payment completed but not successful (e.g., pending, failed)
                flyer.payment_status = "failed"
                flyer.transaction_id = transaction_id
                flyer.updated_at = datetime.now(timezone.utc)

                webhook_log = WebhookLog(
                    flutterwave_event_id=flutterwave_event_id,
                    event_type=event,
                    tx_ref=tx_ref,
                    transaction_id=transaction_id,
                    status="failed",
                    payload_summary=f"Payment status: {payment_status}"
                )
                session.add(webhook_log)
                await session.commit()

                return {"status": "processed", "message": f"Payment {payment_status}", "tx_ref": tx_ref}

        # CRITICAL FIX #3: Handle charge.failed events
        elif event == "charge.failed":
            flyer.payment_status = "failed"
            flyer.transaction_id = transaction_id
            flyer.updated_at = datetime.now(timezone.utc)

            webhook_log = WebhookLog(
                flutterwave_event_id=flutterwave_event_id,
                event_type=event,
                tx_ref=tx_ref,
                transaction_id=transaction_id,
                status="failed",
                payload_summary="Charge failed event received"
            )
            session.add(webhook_log)
            await session.commit()

            print(f"[WEBHOOK] Charge failed for {tx_ref}")
            return {"status": "processed", "message": "Charge failed recorded", "tx_ref": tx_ref}

        else:
            # Unknown event - acknowledge but don't process
            return {"status": "ignored", "message": f"Event {event} not processed"}

    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        # Don't expose internal error details
        print(f"[WEBHOOK] Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")


# =============================================================================
# OTHER ENDPOINTS (Updated with transaction_id)
# =============================================================================

@app.get(
    "/api/flyers/status/{tx_ref}",
    response_model=FlyerStatusResponse,
    tags=["Flyer Generation"]
)
async def check_status(
    tx_ref: str,
    session: AsyncSession = Depends(get_session)
):
    statement = select(FlyerRecord).where(FlyerRecord.tx_ref == tx_ref)
    result = await session.execute(statement)
    flyer = result.scalar_one_or_none()

    if not flyer:
        raise HTTPException(status_code=404, detail=f"No record found: {tx_ref}")

    return FlyerStatusResponse(
        tx_ref=flyer.tx_ref,
        payment_status=flyer.payment_status,
        full_name=flyer.full_name,
        amount=flyer.amount,
        created_at=flyer.created_at
    )


@app.get(
    "/admin/dashboard",
    response_model=AdminDashboardResponse,
    tags=["Admin"],
    dependencies=[Depends(verify_admin_credentials)]
)
async def admin_dashboard(
    session: AsyncSession = Depends(get_session),
    auth: bool = Depends(verify_admin_credentials)
):
    try:
        statement = select(FlyerRecord).order_by(FlyerRecord.created_at.desc())
        result = await session.execute(statement)
        flyers = result.scalars().all()

        total_records = len(flyers)
        successful = sum(1 for f in flyers if f.payment_status == "successful")
        pending = sum(1 for f in flyers if f.payment_status == "pending")
        failed = sum(1 for f in flyers if f.payment_status == "failed")
        revenue = sum(f.amount for f in flyers if f.payment_status == "successful")

        nd2 = sum(1 for f in flyers if f.current_level == "ND2")
        hnd2_swd = sum(1 for f in flyers if f.current_level == "HND2 - SWD")
        hnd2_ncc = sum(1 for f in flyers if f.current_level == "HND2 - NCC")

        single = sum(1 for f in flyers if f.relationship_status.lower() == "single")
        married = sum(1 for f in flyers if f.relationship_status.lower() == "married")
        other_rel = sum(1 for f in flyers if f.relationship_status.lower() not in ["single", "married"])

        flyers_data = []
        for f in flyers:
            birthday = f"{f.birthday_month} {f.birthday_day}".strip() if f.birthday_month or f.birthday_day else "Not specified"

            flyers_data.append(AdminFlyerDetail(
                full_name=f.full_name,
                student_portrait=f.student_portrait,
                nickname=f.nickname,
                state_of_origin=f.state_of_origin,
                birthday=birthday,
                relationship_status=f.relationship_status,
                hobby=f.hobby,
                social_handle=f.social_handle,
                favorite_word_quote=f.favorite_word_quote,
                class_crush=f.class_crush,
                current_level=f.current_level,
                best_level=f.best_level,
                difficult_level=f.difficult_level,
                best_course=f.best_course,
                worst_course=f.worst_course,
                favorite_lecturer=f.favorite_lecturer,
                post_held=f.post_held,
                career_alternative=f.career_alternative,
                business_skill=f.business_skill,
                whats_next=f.whats_next,
                best_campus_experience=f.best_campus_experience,
                tx_ref=f.tx_ref,
                transaction_id=f.transaction_id,
                payment_status=f.payment_status,
                amount=f.amount,
                created_at=f.created_at.isoformat()
            ))

        return AdminDashboardResponse(
            total_records=total_records,
            total_revenue=revenue,
            successful_payments=successful,
            pending_payments=pending,
            failed_payments=failed,
            nd2_count=nd2,
            hnd2_swd_count=hnd2_swd,
            hnd2_ncc_count=hnd2_ncc,
            single_count=single,
            married_count=married,
            other_relationship_count=other_rel,
            flyers=flyers_data
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dashboard error: {str(e)}")


@app.delete(
    "/admin/flyers/{tx_ref}",
    response_model=DeleteResponse,
    tags=["Admin"],
    dependencies=[Depends(verify_admin_credentials)]
)
async def admin_delete_flyer(
    tx_ref: str,
    session: AsyncSession = Depends(get_session),
    auth: bool = Depends(verify_admin_credentials)
):
    try:
        statement = select(FlyerRecord).where(FlyerRecord.tx_ref == tx_ref)
        result = await session.execute(statement)
        flyer = result.scalar_one_or_none()

        if not flyer:
            raise HTTPException(status_code=404, detail=f"No record found: {tx_ref}")

        await session.delete(flyer)
        await session.commit()

        return DeleteResponse(
            success=True,
            message=f"Record {tx_ref} deleted successfully",
            deleted_count=1
        )

    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")


@app.delete(
    "/admin/flyers/bulk/pending",
    response_model=DeleteResponse,
    tags=["Admin"],
    dependencies=[Depends(verify_admin_credentials)]
)
async def admin_delete_all_pending(
    session: AsyncSession = Depends(get_session),
    auth: bool = Depends(verify_admin_credentials)
):
    try:
        statement = select(FlyerRecord).where(FlyerRecord.payment_status == "pending")
        result = await session.execute(statement)
        pending_flyers = result.scalars().all()

        deleted_count = 0
        for flyer in pending_flyers:
            await session.delete(flyer)
            deleted_count += 1

        await session.commit()

        return DeleteResponse(
            success=True,
            message=f"Deleted {deleted_count} pending records",
            deleted_count=deleted_count
        )

    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Bulk delete failed: {str(e)}")


@app.post(
    "/admin/flyers/upload",
    response_model=AdminUploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Admin"],
    dependencies=[Depends(verify_admin_credentials)]
)
async def admin_upload_flyer(
    request: AdminUploadRequest,
    session: AsyncSession = Depends(get_session),
    auth: bool = Depends(verify_admin_credentials)
):
    try:
        tx_ref = generate_tx_ref()

        flyer = FlyerRecord(
            full_name=request.full_name,
            student_portrait=request.student_portrait,
            nickname=request.nickname,
            state_of_origin=request.state_of_origin,
            birthday_month=request.birthday_month,
            birthday_day=request.birthday_day,
            relationship_status=request.relationship_status,
            hobby=request.hobby,
            social_handle=request.social_handle,
            favorite_word_quote=request.favorite_word_quote,
            class_crush=request.class_crush,
            current_level=request.current_level,
            best_level=request.best_level,
            difficult_level=request.difficult_level,
            best_course=request.best_course,
            worst_course=request.worst_course,
            favorite_lecturer=request.favorite_lecturer,
            post_held=request.post_held,
            career_alternative=request.career_alternative,
            business_skill=request.business_skill,
            whats_next=request.whats_next,
            best_campus_experience=request.best_campus_experience,
            tx_ref=tx_ref,
            transaction_id=None,
            payment_status="successful",
            amount=0.0,
            created_at=datetime.now(timezone.utc)
        )

        session.add(flyer)
        await session.commit()
        await session.refresh(flyer)

        birthday = f"{flyer.birthday_month} {flyer.birthday_day}".strip() if flyer.birthday_month or flyer.birthday_day else "Not specified"

        return AdminUploadResponse(
            success=True,
            message="Admin upload successful. No payment required.",
            tx_ref=tx_ref,
            record=AdminFlyerDetail(
                full_name=flyer.full_name,
                student_portrait=flyer.student_portrait,
                nickname=flyer.nickname,
                state_of_origin=flyer.state_of_origin,
                birthday=birthday,
                relationship_status=flyer.relationship_status,
                hobby=flyer.hobby,
                social_handle=flyer.social_handle,
                favorite_word_quote=flyer.favorite_word_quote,
                class_crush=flyer.class_crush,
                current_level=flyer.current_level,
                best_level=flyer.best_level,
                difficult_level=flyer.difficult_level,
                best_course=flyer.best_course,
                worst_course=flyer.worst_course,
                favorite_lecturer=flyer.favorite_lecturer,
                post_held=flyer.post_held,
                career_alternative=flyer.career_alternative,
                business_skill=flyer.business_skill,
                whats_next=flyer.whats_next,
                best_campus_experience=flyer.best_campus_experience,
                tx_ref=flyer.tx_ref,
                transaction_id=flyer.transaction_id,
                payment_status=flyer.payment_status,
                amount=flyer.amount,
                created_at=flyer.created_at.isoformat()
            )
        )

    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Admin upload failed: {str(e)}"
        )


@app.get("/health", tags=["System"])
async def health_check(session: AsyncSession = Depends(get_session)):
    try:
        statement = select(FlyerRecord).limit(1)
        await session.execute(statement)
        return {
            "status": "healthy",
            "database": "connected",
            "auto_delete_pending_hours": settings.AUTO_DELETE_PENDING_HOURS,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Unhealthy: {str(e)}")


@app.get("/", tags=["System"])
async def root():
    return {
        "name": "NACOS Digital Flyer API",
        "version": "3.1.0",
        "features": [
            "Auto-delete pending records after 24h",
            "Admin bulk delete",
            "Admin upload without payment",
            "Flutterwave API verification",
            "Webhook idempotency protection",
            "Audit trail with WebhookLog"
        ],
        "endpoints": {
            "initiate": "POST /api/flyers/initiate",
            "webhook": "POST /api/webhook/flutterwave",
            "status": "GET /api/flyers/status/{tx_ref}",
            "admin_dashboard": "GET /admin/dashboard (Basic Auth)",
            "admin_delete_one": "DELETE /admin/flyers/{tx_ref} (Basic Auth)",
            "admin_delete_pending": "DELETE /admin/flyers/bulk/pending (Basic Auth)",
            "admin_upload": "POST /admin/flyers/upload (Basic Auth)"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT, reload=False, log_level="info")
