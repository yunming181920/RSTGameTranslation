import importlib.util,io,json,copy,threading,http.client,urllib.error,pathlib
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('proxy',pathlib.Path(__file__).resolve().parents[2]/'ollama_proxy.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
p.logf.close();p.log=lambda message:None
seen=[]
def recover(payload):
    seen.append(copy.deepcopy(payload))
    if len(seen)==1:raise p.OllamaGenerationError('prediction aborted, token repeat limit reached')
    return {'response':'你好','done':True,'done_reason':'stop'}
with patch.object(p,'call_ollama_generate',side_effect=recover):
    assert p.translate_native('こんにちは','简体中文','index-translate-2b')=='你好'
assert len(seen)==2 and seen[0]['options']['temperature']==0 and seen[1]['options']['repeat_penalty']>1
assert 256<=seen[0]['options']['num_predict']<=4096
with patch.object(p,'call_ollama_generate',side_effect=p.OllamaGenerationError('token repeat limit reached')) as call:
    try:p.translate_native('あ','简体中文','index-translate-2b');raise AssertionError('accepted failure')
    except p.OllamaGenerationError:pass
    assert call.call_count==2
with patch.object(p,'call_ollama_generate',side_effect=p.OllamaGenerationError('model not found')) as call:
    try:p.translate_native('あ','简体中文','missing');raise AssertionError('accepted failure')
    except p.OllamaGenerationError:pass
    assert call.call_count==1
error=urllib.error.HTTPError('http://localhost',500,'error',{},io.BytesIO(b'{"error":"token repeat limit reached"}'))
with patch.object(p.urllib.request,'urlopen',side_effect=error):
    try:p.call_ollama_generate({});raise AssertionError('accepted failure')
    except p.OllamaGenerationError as e:assert 'token repeat limit reached' in str(e)
with patch.object(p.urllib.request,'urlopen') as call:
    call.return_value.__enter__.return_value.read.return_value=b'{"response":"partial", "done_reason":"length"}'
    try:p.call_ollama_generate({});raise AssertionError('accepted truncated output')
    except p.OllamaGenerationError:pass
server=p.ThreadingHTTPServer(('127.0.0.1',0),p.Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    with patch.object(p,'handle_generate_request',side_effect=p.OllamaGenerationError('token repeat limit reached')),patch.object(p.urllib.request,'urlopen') as upstream:
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        c.request('POST','/api/generate',body='{}',headers={'Content-Type':'application/json'})
        response=c.getresponse();data=json.loads(response.read());c.close()
        assert response.status==502 and 'token repeat limit reached' in data['error']
        upstream.assert_not_called()
finally:server.shutdown();server.server_close();thread.join()
print('PASS: bounded retry, adjusted sampling, output budget, error body, truncation rejection, no raw-prompt fallback')
