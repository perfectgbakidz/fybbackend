"""
Digital Flyer Generation System - FastAPI Backend
Render-Optimized with Full Yearbook Data Model
"""

import os
import secrets
import re
from datetime import datetime, timezone
from typing import Optional, List, Literal
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlmodel import SQLModel, Field as SQLField, create_engine, Session, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from sqlalchemy.orm import sessionmaker


# =============================================================================
# CONFIGURATION
# =============================================================================

class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./flyers.db")
    FLW_SECRET_KEY: str = os.getenv("FLW_SECRET_KEY", "")
    FLW_WEBHOOK_HASH: str = os.getenv("FLW_WEBHOOK_HASH", "")
    ADMIN_USERNAME: str = "admin_nacos"
    ADMIN_PASSWORD: str = "nacos_secure_2024"
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    PORT: int = int(os.getenv("PORT", "8000"))


settings = Settings()


# =============================================================================
# DATABASE MODEL - FULL YEARBOOK SCHEMA
# =============================================================================

class FlyerRecord(SQLModel, table=True):
    __tablename__ = "flyer_records"
    
    id: Optional[int] = SQLField(default=None, primary_key=True)
    
    # 1. CORE IDENTITY
    full_name: str = SQLField(max_length=18)  # Max 17 chars + 1 space
    student_portrait: str = SQLField(sa_column_kwargs={"type_": "TEXT"})  # base64 photo
    
    # 2. PERSONAL & SOCIAL DETAILS
    nickname: str = SQLField(default="")
    state_of_origin: str = SQLField(default="")
    birthday_month: str = SQLField(default="")      # e.g., "March"
    birthday_day: str = SQLField(default="")          # e.g., "15"
    relationship_status: str = SQLField(default="")   # Single, Married, etc.
    hobby: str = SQLField(default="")
    social_handle: str = SQLField(default="")         # @username
    favorite_word_quote: str = SQLField(max_length=20, default="")
    class_crush: str = SQLField(default="")
    
    # 3. ACADEMIC PROFILE
    current_level: str = SQLField(default="")         # ND2, HND2-SWD, HND2-NCC
    best_level: str = SQLField(default="")
    difficult_level: str = SQLField(default="")
    best_course: str = SQLField(default="")
    worst_course: str = SQLField(default="")
    favorite_lecturer: str = SQLField(default="")
    post_held: str = SQLField(default="")             # President, Gen Sec, etc.
    career_alternative: str = SQLField(default="")    # If not CS, what else?
    
    # 4. FUTURE & PROFESSIONAL
    business_skill: str = SQLField(default="")         # Tailoring, Crypto, etc.
    whats_next: str = SQLField(default="")            # Plans after graduation
    best_campus_experience: str = SQLField(sa_column_kwargs={"type_": "TEXT"}, default="")
    
    # PAYMENT & SYSTEM
    tx_ref: str = SQLField(unique=True, index=True)
    payment_status: str = SQLField(default="pending")
    amount: float = SQLField(default=500.0)
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(timezone.utc))


# =============================================================================
# PYDANTIC REQUEST/RESPONSE MODELS
# =============================================================================

class FlyerInitiateRequest(BaseModel):
    model_config = ConfigDict(str_max_length=500)
    
    # 1. CORE IDENTITY
    full_name: str = Field(..., max_length=18, description="Max 17 chars + 1 space")
    student_portrait: str = Field(..., description="Base64 encoded photo")
    
    # 2. PERSONAL & SOCIAL
    nickname: str = Field(default="", description="Campus nickname")
    state_of_origin: str = Field(default="", description="State of origin")
    birthday_month: str = Field(default="", description="Birth month")
    birthday_day: str = Field(default="", description="Birth day (1-31)")
    relationship_status: str = Field(default="Single", description="Single, Married, etc.")
    hobby: str = Field(default="", description="Things you love doing")
    social_handle: str = Field(default="", description="Instagram/X @username")
    favorite_word_quote: str = Field(default="", max_length=20, description="Max 20 characters")
    class_crush: str = Field(default="", description="Favorite person in class")
    
    # 3. ACADEMIC PROFILE
    current_level: Literal["", "ND2", "HND2 - SWD", "HND2 - NCC"] = Field(default="", description="Current academic level")
    best_level: str = Field(default="", description="Most successful academic year")
    difficult_level: str = Field(default="", description="Most challenging level")
    best_course: str = Field(default="", description="Favorite subject")
    worst_course: str = Field(default="", description="Toughest subject")
    favorite_lecturer: str = Field(default="", description="Most impactful lecturer")
    post_held: str = Field(default="", description="Leadership position")
    career_alternative: str = Field(default="", description="If not CS, what else?")
    
    # 4. FUTURE & PROFESSIONAL
    business_skill: str = Field(default="", description="Side hustle/skill")
    whats_next: str = Field(default="", description="Plans after graduation")
    best_campus_experience: str = Field(default="", description="Most memorable MAPOLY moment")
    
    @field_validator('full_name')
    @classmethod
    def validate_full_name(cls, v):
        # Remove extra spaces, check length
        v = re.sub(r'\s+', ' ', v).strip()
        if len(v) > 18:
            raise ValueError("Full name must be maximum 17 characters and 1 space (18 total)")
        # Count spaces - should be exactly 1 (first name + last name)
        if v.count(' ') != 1:
            raise ValueError("Full name must contain exactly one space (FirstName LastName)")
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
    # Core Identity
    full_name: str
    student_portrait: str  # Admin can view photo data
    
    # Personal & Social
    nickname: str
    state_of_origin: str
    birthday: str  # Combined month + day
    relationship_status: str
    hobby: str
    social_handle: str
    favorite_word_quote: str
    class_crush: str
    
    # Academic Profile
    current_level: str
    best_level: str
    difficult_level: str
    best_course: str
    worst_course: str
    favorite_lecturer: str
    post_held: str
    career_alternative: str
    
    # Future & Professional
    business_skill: str
    whats_next: str
    best_campus_experience: str
    
    # Payment Info
    tx_ref: str
    payment_status: str
    amount: float
    created_at: str


class AdminDashboardResponse(BaseModel):
    total_records: int
    total_revenue: float
    successful_payments: int
    pending_payments: int
    failed_payments: int
    # Breakdown by level
    nd2_count: int
    hnd2_swd_count: int
    hnd2_ncc_count: int
    # Breakdown by relationship status
    single_count: int
    married_count: int
    other_relationship_count: int
    # Recent submissions
    flyers: List[AdminFlyerDetail]


# =============================================================================
# DATABASE SETUP
# =============================================================================

engine: AsyncEngine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)
async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


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
# UTILITIES
# =============================================================================

def generate_tx_ref() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random_part = secrets.token_hex(4).upper()
    return f"FLY-{timestamp}-{random_part}"


def generate_payment_link(tx_ref: str) -> str:
    return f"https://checkout.flutterwave.com/v3/hosted-pay?tx_ref={tx_ref}"


# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(
    title="NACOS Digital Flyer API",
    description="Yearbook flyer generation system for MAPOLY students",
    version="2.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL,
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
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
            # Core Identity
            full_name=request.full_name,
            student_portrait=request.student_portrait,
            
            # Personal & Social
            nickname=request.nickname,
            state_of_origin=request.state_of_origin,
            birthday_month=request.birthday_month,
            birthday_day=request.birthday_day,
            relationship_status=request.relationship_status,
            hobby=request.hobby,
            social_handle=request.social_handle,
            favorite_word_quote=request.favorite_word_quote,
            class_crush=request.class_crush,
            
            # Academic Profile
            current_level=request.current_level,
            best_level=request.best_level,
            difficult_level=request.difficult_level,
            best_course=request.best_course,
            worst_course=request.worst_course,
            favorite_lecturer=request.favorite_lecturer,
            post_held=request.post_held,
            career_alternative=request.career_alternative,
            
            # Future & Professional
            business_skill=request.business_skill,
            whats_next=request.whats_next,
            best_campus_experience=request.best_campus_experience,
            
            # Payment
            tx_ref=tx_ref,
            payment_status="pending",
            amount=500.0,
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
    """Handle Flutterwave payment confirmation."""
    if not verify_webhook_signature(verif_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    
    try:
        event = payload.get("event", "")
        data = payload.get("data", {})
        
        if event != "charge.completed":
            return {"status": "ignored", "message": f"Event {event} not processed"}
        
        payment_status = data.get("status", "").lower()
        tx_ref = data.get("tx_ref")
        
        if not tx_ref:
            raise HTTPException(status_code=400, detail="Missing transaction reference")
        
        statement = select(FlyerRecord).where(FlyerRecord.tx_ref == tx_ref)
        result = await session.execute(statement)
        flyer = result.scalar_one_or_none()
        
        if not flyer:
            raise HTTPException(status_code=404, detail=f"Record {tx_ref} not found")
        
        if payment_status == "successful":
            flyer.payment_status = "successful"
            await session.commit()
            return {"status": "success", "message": "Payment confirmed", "tx_ref": tx_ref}
        else:
            flyer.payment_status = "failed"
            await session.commit()
            return {"status": "processed", "message": f"Payment {payment_status}", "tx_ref": tx_ref}
            
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Webhook failed: {str(e)}")


@app.get(
    "/api/flyers/status/{tx_ref}",
    response_model=FlyerStatusResponse,
    tags=["Flyer Generation"]
)
async def check_status(
    tx_ref: str,
    session: AsyncSession = Depends(get_session)
):
    """Check payment status by tx_ref."""
    statement = select(FlyerRecord).where(FlyerRecord.tx_ref == tx_ref)
    result = await session.execute(statement)
    flyer = result.scalar_one_or_none()
    
    if not flyer:
        raise HTTPException(status_code=404, detail=f"No record found: {tx_ref}")
    
    return FlyerStatusResponse(
        tx_ref=flyer.tx_ref,
        payment_status=flyer.payment_status,  # type: ignore
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
    """
    Admin dashboard with complete student data retrieval.
    Protected by HTTP Basic Auth.
    """
    try:
        statement = select(FlyerRecord).order_by(FlyerRecord.created_at.desc())
        result = await session.execute(statement)
        flyers = result.scalars().all()
        
        # Statistics
        total_records = len(flyers)
        successful = sum(1 for f in flyers if f.payment_status == "successful")
        pending = sum(1 for f in flyers if f.payment_status == "pending")
        failed = sum(1 for f in flyers if f.payment_status == "failed")
        revenue = sum(f.amount for f in flyers if f.payment_status == "successful")
        
        # Level breakdown
        nd2 = sum(1 for f in flyers if f.current_level == "ND2")
        hnd2_swd = sum(1 for f in flyers if f.current_level == "HND2 - SWD")
        hnd2_ncc = sum(1 for f in flyers if f.current_level == "HND2 - NCC")
        
        # Relationship breakdown
        single = sum(1 for f in flyers if f.relationship_status.lower() == "single")
        married = sum(1 for f in flyers if f.relationship_status.lower() == "married")
        other_rel = sum(1 for f in flyers if f.relationship_status.lower() not in ["single", "married"])
        
        # Format detailed records
        flyers_data = []
        for f in flyers:
            # Combine birthday fields
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


@app.get("/health", tags=["System"])
async def health_check(session: AsyncSession = Depends(get_session)):
    try:
        statement = select(FlyerRecord).limit(1)
        await session.execute(statement)
        return {
            "status": "healthy",
            "database": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Unhealthy: {str(e)}")


@app.get("/", tags=["System"])
async def root():
    return {
        "name": "NACOS Digital Flyer API",
        "version": "2.0.0",
        "endpoints": {
            "initiate": "POST /api/flyers/initiate",
            "webhook": "POST /api/webhook/flutterwave",
            "status": "GET /api/flyers/status/{tx_ref}",
            "admin": "GET /admin/dashboard (Basic Auth)"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.PORT, reload=False, log_level="info")
