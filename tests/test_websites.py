import uuid
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.modules.websites.router import (WebsiteUpdatePayload, TEMPLATES, update_organization_website,
    publish_website, get_website_editor, get_public_organization_website)
from app.modules.organizations.dependencies import require_org_role, get_current_tenant_member
from app.common.enums import UserRole

@pytest.mark.parametrize('code', list(TEMPLATES))
def test_all_templates(code):
    assert WebsiteUpdatePayload(template_code=code, headline='Hello').template_code == code

@pytest.mark.parametrize('fields', [dict(headline=' '), dict(headline='Hello',template_code='invented'), dict(headline='Hello',hero_image_url='javascript:alert(1)'), dict(headline='Hello',primary_color='red')])
def test_invalid_content(fields):
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(**fields)

@pytest.mark.asyncio
async def test_draft_publish_and_private_edits(db_session):
    org = Organization(name='Website Studio', slug=f'website-{uuid.uuid4().hex}')
    db_session.add(org)
    await db_session.flush()
    tenant = (org, None)
    payload = WebsiteUpdatePayload(template_code='MUSIC_STUDIO',headline='Private first draft')
    await update_organization_website(org.id, payload, tenant, db_session)
    editor = await get_website_editor(org.id, tenant, db_session)
    assert editor['content']['headline'] == 'Private first draft'
    assert not editor['is_published']
    public = await get_public_organization_website(org.slug, db_session)
    assert public['website']['headline'] != 'Private first draft'
    await publish_website(org.id, tenant, db_session)
    public = await get_public_organization_website(org.slug, db_session)
    assert public['website']['headline'] == 'Private first draft'
    assert public['website']['is_published']
    payload.headline = 'Unpublished changes'
    await update_organization_website(org.id, payload, tenant, db_session)
    public = await get_public_organization_website(org.slug, db_session)
    assert public['website']['headline'] == 'Private first draft'
    assert 'custom_sections' not in public['website']

@pytest.mark.asyncio
async def test_student_cannot_edit():
    member = OrganizationMember(role='STUDENT')
    with pytest.raises(HTTPException) as error:
        await require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.INSTRUCTOR])((None, member))
    assert error.value.status_code == 403

@pytest.mark.asyncio
async def test_cross_tenant_editor_denied(db_session):
    org = Organization(name='Other studio', slug=f'other-{uuid.uuid4().hex}')
    user = User(email=f'{uuid.uuid4().hex}@test.local', full_name='Unrelated teacher')
    db_session.add_all([org, user])
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await get_current_tenant_member(org.id, user, db_session)
    assert error.value.status_code == 403

@pytest.mark.asyncio
async def test_image_upload_and_retrieval(tmp_path, monkeypatch):
    import io
    from fastapi import UploadFile
    from app.modules.websites import router as websites
    monkeypatch.setattr(websites, 'MEDIA_DIR', tmp_path)
    png = b'\x89PNG\r\n\x1a\n' + b'test-image'
    result = await websites.upload_website_image(uuid.uuid4(), UploadFile(filename='photo.png',file=io.BytesIO(png)), (None,None))
    WebsiteUpdatePayload(headline='Image test',hero_image_url=result['url'])
    filename = result['url'].rsplit('/',1)[-1]
    response = await websites.website_image(filename)
    assert response.path.read_bytes() == png
    assert response.headers['x-content-type-options'] == 'nosniff'
    with pytest.raises(HTTPException):
        await websites.website_image('../secret.txt')
    with pytest.raises(HTTPException):
        await websites.upload_website_image(uuid.uuid4(), UploadFile(filename='bad.svg',file=io.BytesIO(b'<svg/>')), (None,None))
    with pytest.raises(HTTPException) as error:
        await websites.upload_website_image(uuid.uuid4(), UploadFile(filename='large.png',file=io.BytesIO(b'x'*(5*1024*1024+1))), (None,None))
    assert error.value.status_code == 413

@pytest.fixture(autouse=True)
def isolated_exports(tmp_path, monkeypatch):
    from app.modules.websites import rendering
    monkeypatch.setattr(rendering, 'EXPORT_DIR', tmp_path / 'exports')


def test_five_image_and_nine_card_limits():
    from app.modules.websites.schemas import PRESET_PREFIX
    images = [dict(id=f'image-{i}',url=f'{PRESET_PREFIX}movement.jpg') for i in range(5)]
    cards = [dict(title=f'Card {i}',image_id='image-0') for i in range(9)]
    valid = dict(headline='A real studio',images=images,sections=[dict(id='offerings',title='Offerings',cards=cards)])
    assert len(WebsiteUpdatePayload(**valid).sections[0].cards) == 9
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(**{**valid,'images':images+[dict(id='sixth',url=f'{PRESET_PREFIX}movement.jpg')]})
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(**{**valid,'sections':[dict(id='offerings',title='Offerings',cards=cards+[dict(title='Tenth card')])]})
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(headline='Test',images=images,sections=[dict(id='s',title='Title',cards=[dict(title='Missing image',image_id='missing')])])
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(headline='Test',images=[images[0],images[0]])
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(headline='Test',sections=[dict(id='duplicate',title='One'),dict(id='duplicate',title='Two')])
    assert WebsiteUpdatePayload(headline='Test',sections=[],images=[]).images == []


@pytest.mark.parametrize('url', ['https://127.0.0.1/secret','https://169.254.169.254/latest/meta-data','https://evil.example/image.jpg','/api/v1/public/website-images/../../secret.png','file:///etc/passwd'])
def test_image_sources_cannot_trigger_arbitrary_server_requests(url):
    with pytest.raises(ValidationError):
        WebsiteUpdatePayload(headline='Safe',images=[dict(id='image',url=url)])


@pytest.mark.asyncio
@pytest.mark.parametrize('code', list(TEMPLATES))
async def test_every_template_is_self_contained_and_escapes_content(code):
    from app.modules.websites.catalog import DESIGNS, sample_content
    from app.modules.websites.rendering import render_document
    from html.parser import HTMLParser
    content = sample_content(next(item for item in DESIGNS if item['code'] == code))
    content['headline'] = '<script>alert("xss")</script>'
    content['sections'][0]['cards'][0]['title'] = '<img src=x onerror=alert(1)>'
    html = await render_document(WebsiteUpdatePayload(**content), 'Studio <unsafe>', 'my-studio', 'https://progressrooms.example')
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    assert 'https://progressrooms.example/my-studio/explore' in html
    assert 'src="http' not in html and 'src="/api' not in html
    assert html.count("url('data:image/") == len({image['url'] for image in content['images']})
    assert '<style>' in html and "url('data:image/" in html
    assert '@media(max-width:600px)' in html
    class Assets(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'img':
                assert values['src'].startswith('data:image/')
            assert tag != 'script'
            if tag == 'link':
                assert values.get('rel') != 'stylesheet'
    Assets().feed(html)


@pytest.mark.asyncio
async def test_published_artifact_is_immutable_and_survives_without_app(db_session):
    from app.modules.websites.router import preview_website, download_website, public_website_html
    from app.modules.websites.rendering import version_path
    from app.modules.websites.catalog import sample_content, DESIGNS
    org = Organization(name='Portable studio',slug=f'portable-{uuid.uuid4().hex}')
    db_session.add(org)
    await db_session.flush()
    tenant=(org,None)
    content=WebsiteUpdatePayload(**sample_content(DESIGNS[0]))
    preview=await preview_website(org.id,content,tenant)
    assert 'data:image/jpeg;base64,' in preview['html']
    assert version_path(str(org.id),preview['version'],'previews').read_text() == preview['html']
    await update_organization_website(org.id,content,tenant,db_session)
    first=await publish_website(org.id,tenant,db_session)
    path=version_path(str(org.id),first['release']['version'])
    first_bytes=path.read_bytes()
    content.headline='A second published version'
    await update_organization_website(org.id,content,tenant,db_session)
    response=await public_website_html(org.slug,db_session)
    assert response.body == first_bytes
    second=await publish_website(org.id,tenant,db_session)
    assert second['release']['version'] != first['release']['version']
    assert path.read_bytes() == first_bytes
    assert (await public_website_html(org.slug,db_session)).body != first_bytes
    download=await download_website(org.id,first['release']['version'],tenant,db_session)
    assert download.path.read_bytes() == first_bytes
    # The artifact's HTML/styles/images are complete disk bytes; no application
    # imports, fetch, JS bundles, service worker, or remote assets are needed to view it.
    assert b'<script' not in first_bytes and b'/_next/' not in first_bytes
    assert b'src="http' not in first_bytes
    with pytest.raises(HTTPException) as error:
        await download_website(org.id,preview['version'],tenant,db_session)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_missing_image_cannot_replace_live_release(db_session):
    from app.modules.websites.router import public_website_html
    org=Organization(name='Protected release',slug=f'protected-{uuid.uuid4().hex}')
    db_session.add(org)
    await db_session.flush()
    tenant=(org,None)
    content=WebsiteUpdatePayload(headline='Keep this live')
    await update_organization_website(org.id,content,tenant,db_session)
    await publish_website(org.id,tenant,db_session)
    original=(await public_website_html(org.slug,db_session)).body
    broken=WebsiteUpdatePayload(headline='Do not publish',images=[dict(id='missing',url=f'/api/v1/public/website-images/{org.id}-{uuid.uuid4()}.png')])
    await update_organization_website(org.id,broken,tenant,db_session)
    with pytest.raises(HTTPException) as error:
        await publish_website(org.id,tenant,db_session)
    assert error.value.status_code == 422
    assert (await public_website_html(org.slug,db_session)).body == original


@pytest.mark.asyncio
async def test_foreign_tenant_image_cannot_be_embedded():
    from app.modules.websites.rendering import render_document
    content=WebsiteUpdatePayload(headline='Test',images=[dict(id='other',url=f'/api/v1/public/website-images/{uuid.uuid4()}-{uuid.uuid4()}.png')])
    with pytest.raises(HTTPException) as error:
        await render_document(content,'Name','slug','http://localhost:3000',str(uuid.uuid4()))
    assert error.value.status_code == 403
