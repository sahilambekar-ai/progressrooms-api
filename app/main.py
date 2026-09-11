from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.modules.auth.router import router as auth_router
from app.modules.organizations.router import router as org_router
from app.modules.plans.router import router as plan_router
from app.modules.classes.router import router as class_router
from app.modules.scheduling.router import router as schedule_router
from app.modules.commerce.router import router as commerce_router
from app.modules.attendance.router import router as attendance_router
from app.modules.websites.router import router as website_router
from app.modules.student.router import router as student_router
from app.modules.students.router import router as students_directory_router
from app.modules.teachers.router import router as teachers_router
from app.modules.admin.router import router as admin_router

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
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
api_v1 = FastAPI()
api_v1.include_router(auth_router)
api_v1.include_router(org_router)
api_v1.include_router(plan_router)
api_v1.include_router(class_router)
api_v1.include_router(schedule_router)
api_v1.include_router(commerce_router)
api_v1.include_router(attendance_router)
api_v1.include_router(website_router)
api_v1.include_router(student_router)
api_v1.include_router(students_directory_router)
api_v1.include_router(teachers_router)
api_v1.include_router(admin_router)

app.mount(settings.API_V1_STR, api_v1)

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "Welcome to ProgressRooms Multi-Tenant API",
        "docs": f"{settings.API_V1_STR}/docs"
    }
