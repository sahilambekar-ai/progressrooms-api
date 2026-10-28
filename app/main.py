from fastapi import FastAPI, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.modules.zoom.router import router as zoom_router
from app.modules.auth.router import router as auth_router
from app.modules.organizations.router import router as org_router
from app.modules.plans.router import router as plan_router
from app.modules.classes.router import router as class_router
from app.modules.scheduling.router import router as schedule_router
from app.modules.commerce.router import router as commerce_router
from app.modules.attendance.router import router as attendance_router
from app.modules.attendance.calendar import router as attendance_calendar_router
from app.modules.websites.router import router as website_router
from app.modules.student.router import router as student_router
from app.modules.students.router import router as students_directory_router
from app.modules.teachers.router import router as teachers_router
from app.modules.admin.router import router as admin_router
from app.modules.membership_tiers.router import router as membership_tiers_router
from app.modules.billing.router import router as billing_router
from app.modules.billing.saas_router import router as saas_billing_router
from app.modules.support.router import router as support_router
from app.modules.communication.router import router as communication_router

from app.modules.admin.health import lifespan
from app.core.database import get_db

app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if settings.is_local_environment else None,
    docs_url=f"{settings.API_V1_STR}/docs" if settings.is_local_environment else None,
    redoc_url=f"{settings.API_V1_STR}/redoc" if settings.is_local_environment else None,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include Routers under API_V1_STR
api_v1 = FastAPI(openapi_url="/openapi.json" if settings.is_local_environment else None, docs_url="/docs" if settings.is_local_environment else None, redoc_url="/redoc" if settings.is_local_environment else None)
api_v1.include_router(zoom_router)
api_v1.include_router(auth_router)
api_v1.include_router(org_router)
api_v1.include_router(plan_router)
api_v1.include_router(class_router)
api_v1.include_router(membership_tiers_router)
api_v1.include_router(billing_router)
api_v1.include_router(saas_billing_router)
api_v1.include_router(schedule_router)
api_v1.include_router(commerce_router)
api_v1.include_router(attendance_router)
api_v1.include_router(attendance_calendar_router)
api_v1.include_router(website_router)
api_v1.include_router(student_router)
api_v1.include_router(students_directory_router)
api_v1.include_router(teachers_router)
api_v1.include_router(admin_router)
api_v1.include_router(support_router)
api_v1.include_router(communication_router)

app.mount(settings.API_V1_STR, api_v1)

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "Welcome to Progress Rooms Multi-Tenant API",
        "docs": f"{settings.API_V1_STR}/docs" if settings.is_local_environment else None
    }


from app.core.observability import request_metrics, unhandled_error
app.middleware("http")(request_metrics)
app.add_exception_handler(Exception, unhandled_error)

@app.get("/ready", tags=["Health"], include_in_schema=False)
async def ready(db=Depends(get_db)):
    import asyncio
    from sqlalchemy import text
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2)
    except Exception:
        return JSONResponse({"status":"unavailable"},status_code=503)
    return {"status":"ready"}
