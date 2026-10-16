from .schemas import TEMPLATES, PRESET_PREFIX

DESIGNS = [
    {"code": "TEMPLATE_1", "description": "An elegant editorial layout with oversized type and a sculptural photo composition.", "layout": "editorial", "ink": "#253c32", "background": "#f7f3e9", "accent": "#d6dfc6", "headline": "Find a little more balance.", "eyebrow": "Slow down. Feel more.", "photos": ["movement", "stillness", "learning"]},
    {"code": "TEMPLATE_2", "description": "A bold, full-screen cover. Confident typography and a luminous lime accent.", "layout": "bold", "ink": "#f4f4ec", "background": "#181c19", "accent": "#d9f47d", "headline": "Make room for your potential.", "eyebrow": "Your energy. Your space.", "photos": ["sound", "movement", "community"]},
    {"code": "TEMPLATE_3", "description": "A playful collage with soft shapes, lilac tones, and colorful feature cards.", "layout": "collage", "ink": "#41335f", "background": "#f4effb", "accent": "#e9ac67", "headline": "Good things grow together.", "eyebrow": "A fresh perspective", "photos": ["learning", "community", "movement"]},
    {"code": "TEMPLATE_4", "description": "Immersive photography, a floating navigation bar, and a calm forest palette.", "layout": "immersive", "ink": "#f5f1e7", "background": "#12392e", "accent": "#e4cb9c", "headline": "A world of possibility. A moment for you.", "eyebrow": "Reconnect with what matters", "photos": ["stillness", "movement", "community"]},
    {"code": "TEMPLATE_5", "description": "A warm journal-inspired design with an editorial masthead and generous storytelling.", "layout": "journal", "ink": "#582f25", "background": "#f8e9de", "accent": "#bf6045", "headline": "Practice the art of showing up.", "eyebrow": "Small rituals. Lasting change.", "photos": ["movement", "learning", "stillness"]},
    {"code": "TEMPLATE_6", "description": "A crisp modern grid with striking blue, modular panels, and punchy type.", "layout": "grid", "ink": "#173cbc", "background": "#f1f4ef", "accent": "#daee83", "headline": "Your next great beginning.", "eyebrow": "Make progress, together", "photos": ["community", "sound", "learning"]},
]


def sample_content(design):
    return {
        "template_code": design["code"], "eyebrow": design["eyebrow"],
        "headline": design["headline"],
        "subheadline": "Thoughtful guidance, inspiring classes, and a community that brings out the best in you. Come as you are. Start where you are.",
        "about_title": "A space to explore. A place to belong.",
        "about_text": "We believe the best kind of progress begins with a little curiosity. Our studio brings people together through personal guidance, shared experiences, and the joy of learning something new.\n\nWhether this is your very first step or part of a lifelong practice, there is room for you here.",
        "cta_title": "Let's begin something good.",
        "cta_text": "Discover a class that fits your life. We will meet you where you are.",
        "images": [{"id": f"image-{i+1}", "url": f"{PRESET_PREFIX}{name}.jpg", "alt": f"A moment of {name} at the studio"} for i, name in enumerate(design["photos"])],
        "sections": [
            {"id": "experiences", "title": "Find what moves you.", "description": "A little inspiration for wherever you are in your journey.", "cards": [
                {"title": "Begin with the essentials", "text": "Build a strong foundation with welcoming guidance and a pace that feels right for you.", "image_id": "image-1"},
                {"title": "Grow at your own pace", "text": "Go a little deeper, explore new possibilities, and make space for everyday progress.", "image_id": "image-2"},
                {"title": "Make it a shared experience", "text": "Connect with like-minded people through small groups and inspiring workshops.", "image_id": "image-3"},
            ]},
            {"id": "approach", "title": "Made for real life.", "description": "Good teaching is personal. Here is what you can expect from us.", "cards": [
                {"title": "Thoughtful guidance", "text": "A teacher who listens, encourages, and helps you find your way."},
                {"title": "A welcoming space", "text": "Come with questions, bring your curiosity, and feel at home."},
                {"title": "Meaningful progress", "text": "Small, consistent steps toward something that matters to you."},
            ]},
        ],
        "primary_color": design["ink"], "accent_color": design["accent"],
    }


def catalog():
    return [{**{k: v for k, v in design.items() if k not in ("photos", "headline", "eyebrow")}, "name": TEMPLATES[design["code"]], "content": sample_content(design)} for design in DESIGNS]
