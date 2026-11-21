"""The retired embedded UI must not be served by the unified admin SPA."""
from types import SimpleNamespace
from fastapi.testclient import TestClient
import pytest
from smsinvit.app import Services,create_app
from smsinvit.config import Config
from smsinvit.security import Principal


@pytest.fixture
def client(tmp_path):
    static=tmp_path/'static';(static/'admin/invitation/native/static').mkdir(parents=True)
    (static/'index.html').write_text('<html>public frontend</html>')
    (static/'admin/index.html').write_text('<html>unified admin</html>')
    # Even accidentally retained files must be rejected before static fallback.
    (static/'admin/invitation/native/static/retired.js').write_text('retired UI')
    retired=tmp_path/'retired';retired.mkdir();(retired/'index.html').write_text('<html>old invitation</html>')
    config=Config({'smsinvit.inventory-encryption-key':'a1'*32,'smsinvit.inventory-fingerprint-key':'native-page-test-key','smsinvit.jwt-secret':'native-page-test-jwt-secret-over32chars','SMSINVIT_STATIC_DIR':str(static),'SMSINVIT_INVITATION_STATIC_DIR':str(retired)},environ={})
    db=SimpleNamespace(one=lambda *args:None,all=lambda *args:[],execute=lambda *args:None)
    services=Services(db,config);services.limiter=SimpleNamespace(check=lambda *args:None)
    services.auth.authenticate=lambda *args:Principal('test-actor','test-session',('ADMIN',))
    app=create_app(config,db,services=services,include_domains=False,load_runtime=False)
    @app.get('/api/v1/admin/invitation/native/api/overview')
    def native_json():return {'ok':True,'stats':{'total':7}}
    # Production business routes precede the SPA catch-all; preserve that
    # ordering for this mocked route installed after create_app returns.
    app.router.routes.insert(0,app.router.routes.pop())
    with TestClient(app) as test_client:yield test_client


@pytest.mark.parametrize('path',['/admin/invitation/native','/admin/invitation/native/'])
def test_old_home_redirects_to_unified_admin_without_serving_html(client,path):
    response=client.get(path,follow_redirects=False)
    assert response.status_code==308
    assert response.headers['location']=='/admin/invitation'
    assert response.headers['x-frame-options']=='DENY'
    assert response.headers['cache-control']=='no-store'
    assert 'old invitation' not in response.text


@pytest.mark.parametrize('path',[
    '/admin/invitation/native/static/retired.js',
    '/admin/invitation/native/static/missing.js',
    '/admin/invitation/native/assets/old.js',
    '/admin/invitation/native/index.html',
    '/admin/invitation/native/runs',
])
def test_retired_native_descendants_never_fall_through_to_spa(client,path):
    response=client.get(path)
    assert response.status_code==404
    assert response.json()['error']['code']=='NOT_FOUND'
    assert response.headers['x-frame-options']=='DENY'
    assert 'unified admin' not in response.text and 'retired UI' not in response.text


@pytest.mark.parametrize('path',['/admin/invitation','/admin/invitation/register','/admin/invitation/registered','/admin/invitation/cdk','/admin/invitation/sms-prices','/admin/invitation/legacy'])
def test_native_admin_deep_links_keep_unified_spa_and_deny_embedding(client,path):
    response=client.get(path)
    assert response.status_code==200 and 'unified admin' in response.text
    assert response.headers['x-frame-options']=='DENY'


def test_native_json_interface_remains_authenticated_and_unembeddable(client):
    url='/api/v1/admin/invitation/native/api/overview'
    assert client.get(url).status_code==401
    response=client.get(url,headers={'Authorization':'Bearer fixture'})
    assert response.status_code==200 and response.json()=={'ok':True,'stats':{'total':7}}
    assert response.headers['x-frame-options']=='DENY'
