import hashlib,uuid
from .test_v24_api_auth import secure_client,login,create

def test_real_put_get_hash_version_acl_and_bad_hash(secure_client):
    a=login(secure_client);job=create(secure_client,a);b=login(secure_client,'mallory')
    path='/files/'+job['project_id']+'/'+uuid.uuid4().hex+'.txt';raw='河南真实资料上传测试'.encode()
    h=hashlib.sha256(raw).hexdigest();r=secure_client.put(path,content=raw,headers={**a,'X-Content-SHA256':h})
    assert r.status_code==200,r.text
    assert r.json()['file_sha256']==h
    assert secure_client.get(path,headers=a).content==raw
    assert secure_client.get(path,headers=b).status_code==403
    assert secure_client.put(path,content=raw,headers=a).json()['version']==r.json()['version']
    assert secure_client.put(path,content=b'changed',headers=a).json()['version']==r.json()['version']+1
    assert secure_client.put(path,content=b'bad',headers={**a,'X-Content-SHA256':'0'*64}).status_code==422
    assert secure_client.get(path,headers=a).content==b'changed'
    assert secure_client.put('/files/'+job['project_id']+'/%2e%2e/escape.txt',content=raw,headers=a).status_code==403
