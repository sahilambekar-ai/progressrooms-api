from enum import Enum

class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    INSTRUCTOR = "INSTRUCTOR"
    STUDENT = "STUDENT"

class OrgStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"

class SubscriptionStatus(str, Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SUSPENDED = "SUSPENDED"

class SessionType(str, Enum):
    GROUP = "GROUP"
    ONE_TO_ONE = "ONE_TO_ONE"
    WORKSHOP = "WORKSHOP"
    PROGRAM = "PROGRAM"

class SkillLevel(str, Enum):
    ALL_LEVELS = "ALL_LEVELS"
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"

class PricingType(str, Enum):
    ONE_TIME = "ONE_TIME"
    MONTHLY = "MONTHLY"
    PACKAGE = "PACKAGE"
    PER_SESSION = "PER_SESSION"

class InstructorRole(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    SUBSTITUTE = "SUBSTITUTE"

class OccurrenceStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"

class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"

class EnrollmentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

class AttendanceStatus(str, Enum):
    ATTENDED = "ATTENDED"
    ABSENT = "ABSENT"
    EXCUSED = "EXCUSED"

class WebsiteTemplateCode(str, Enum):
    DEFAULT = "DEFAULT"
    TEMPLATE_A = "TEMPLATE_A"
    TEMPLATE_B = "TEMPLATE_B"
    TEMPLATE_C = "TEMPLATE_C"
