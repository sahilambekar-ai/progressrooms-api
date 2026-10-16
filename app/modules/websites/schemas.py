"""The one-page website contract, shared by preview, publication, and export."""
import re
from pydantic import BaseModel, Field, field_validator, model_validator

TEMPLATES = {f"TEMPLATE_{i}": f"Template {i}" for i in range(1, 7)}
LEGACY_CODES = dict(zip(
    ["YOGA_STILL", "YOGA_EARTH", "MUSIC_STUDIO", "MUSIC_COLLECTIVE", "EDUCATION_PAPER", "EDUCATION_SPARK"],
    TEMPLATES,
))
LEGACY_CODES.update({"DEFAULT": "TEMPLATE_1", "TEMPLATE_A": "TEMPLATE_2", "TEMPLATE_B": "TEMPLATE_3", "TEMPLATE_C": "TEMPLATE_4"})
PRESET_NAMES = {"movement", "stillness", "sound", "learning", "community"}
IMAGE_PREFIX = "/api/v1/public/website-images/"
PRESET_PREFIX = "/api/v1/public/website-assets/"
MAX_IMAGES = 5
MAX_CARDS = 9
MAX_SECTIONS = 12


def safe_image_url(value: str) -> str:
    if not value:
        return value
    if re.fullmatch(r"/api/v1/public/website-images/(?:[0-9a-f-]{36}-)?[0-9a-f-]{36}\.(png|jpg|webp)", value):
        return value
    if value in {f"{PRESET_PREFIX}{name}.jpg" for name in PRESET_NAMES}:
        return value
    # Existing drafts used these stock URLs. The renderer only downloads from this
    # single fixed host, never arbitrary URLs or redirects (SSRF protection).
    if re.fullmatch(r"https://images\.unsplash\.com/photo-[a-zA-Z0-9-]+(?:\?[a-zA-Z0-9_=&%.,-]+)?", value):
        return value
    raise ValueError("Upload an image or choose a sample photo")


class WebsiteImage(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    url: str = Field(min_length=1, max_length=1024)
    alt: str = Field(default="", max_length=200)
    _safe_url = field_validator("url")(safe_image_url)


class WebsiteCard(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=1000)
    image_id: str | None = None

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Add a card title")
        return value.strip()


class WebsiteSection(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1200)
    cards: list[WebsiteCard] = Field(default_factory=list, max_length=MAX_CARDS)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Add a section title")
        return value.strip()


class WebsiteUpdatePayload(BaseModel):
    template_code: str = "TEMPLATE_1"
    eyebrow: str = Field(default="A little space for you", max_length=100)
    headline: str = Field(min_length=1, max_length=255)
    subheadline: str = Field(default="", max_length=500)
    about_title: str = Field(default="More than a studio. A place to belong.", max_length=180)
    about_text: str = Field(default="", max_length=5000)
    cta_title: str = Field(default="Your next chapter starts here.", max_length=180)
    cta_text: str = Field(default="Find your class. Make a little time for yourself.", max_length=500)
    images: list[WebsiteImage] = Field(default_factory=list, max_length=MAX_IMAGES)
    sections: list[WebsiteSection] = Field(default_factory=list, max_length=MAX_SECTIONS)
    # Retained for older clients and the existing relational website columns.
    hero_image_url: str = Field(default="", max_length=1024)
    primary_color: str = Field(default="#243c32", pattern=r"^#[0-9a-fA-F]{6}$")
    accent_color: str = Field(default="#d4c7aa", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value["template_code"] = LEGACY_CODES.get(value.get("template_code"), value.get("template_code", "TEMPLATE_1"))
            if "images" not in value and value.get("hero_image_url"):
                value["images"] = [{"id": "legacy-hero", "url": value["hero_image_url"], "alt": "Studio photograph"}]
        return value

    @field_validator("template_code")
    @classmethod
    def template_exists(cls, value):
        if value not in TEMPLATES:
            raise ValueError("Choose Template 1–6")
        return value

    @field_validator("headline")
    @classmethod
    def headline_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Add a headline")
        return value.strip()

    _safe_hero = field_validator("hero_image_url")(safe_image_url)

    @model_validator(mode="after")
    def validate_references(self):
        ids = [image.id for image in self.images]
        if len(set(ids)) != len(ids):
            raise ValueError("Image identifiers must be unique")
        section_ids = [section.id for section in self.sections]
        if len(set(section_ids)) != len(section_ids):
            raise ValueError("Section identifiers must be unique")
        for section in self.sections:
            for card in section.cards:
                if card.image_id is not None and card.image_id not in ids:
                    raise ValueError("A card refers to an image that is no longer in the library")
        self.hero_image_url = self.images[0].url if self.images else ""
        return self
