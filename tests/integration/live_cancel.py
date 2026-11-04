import concurrent.futures, importlib.util, json, pathlib, threading, time, urllib.request, urllib.error, uuid

ROOT=pathlib.Path(__file__).resolve().parent
OUT=ROOT.parent/'artifacts';OUT.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('cancel_test_proxy',ROOT.parent.parent/'ollama_proxy.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);p.logf.close();p.log=lambda msg:None
server=p.ThreadingHTTPServer(('127.0.0.1',0),p.Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
base=f'http://127.0.0.1:{server.server_port}'
fixture=json.loads((ROOT.parent/'fixtures/game-dialogue.json').read_text(encoding='utf-8'))

def post(path,body):
    request=urllib.request.Request(base+path,data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
    start=time.perf_counter()
    try:
        with urllib.request.urlopen(request,timeout=50) as response: return {'status':response.status,'body':json.load(response),'ms':(time.perf_counter()-start)*1000}
    except urllib.error.HTTPError as exc: return {'status':exc.code,'body':json.load(exc),'ms':(time.perf_counter()-start)*1000}

def body(request_id,texts):
    return {'model':'index-translate-2b','_rst':{'request_id':request_id},
            'prompt':json.dumps({'source_language':'Japanese','target_language':'Chinese (Simplified)',
                                 'text_blocks':[{'id':str(i),'text':text} for i,text in enumerate(texts)]},ensure_ascii=False)}

rows=[]
try:
    # A tombstone must also cancel a request that arrives just after its cancel notification.
    tombstone=uuid.uuid4().hex
    post('/rst/cancel',{'request_id':tombstone})
    early=post('/api/generate',body(tombstone,['体力回復']))
    assert early['status']==499,early
    for trial in range(3):
        p._RESULT_CACHE.clear();p._PIECE_CACHE.clear()
        old_id=uuid.uuid4().hex;new_id=uuid.uuid4().hex
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            old=pool.submit(post,'/api/generate',body(old_id,fixture['12_long']))
            deadline=time.monotonic()+5
            while old_id not in p._ACTIVE_REQUESTS and time.monotonic()<deadline:time.sleep(.005)
            time.sleep(.15)
            cancel=post('/rst/cancel',{'request_id':old_id})
            fresh=pool.submit(post,'/api/generate',body(new_id,['体力が満タンの時は、このアイテムを使えません。']))
            old_result=old.result();new_result=fresh.result()
        assert old_result['status']==499,old_result
        assert new_result['status']==200,new_result
        answer=json.loads(new_result['body']['response'])['text_blocks'][0]['text']
        assert not any('\u3040'<=c<='\u309f' for c in answer),answer
        row={'trial':trial,'old_status':old_result['status'],'old_total_ms':old_result['ms'],'cancel_ms':cancel['ms'],
             'new_ms':new_result['ms'],'new_translation':answer,'active_requests_after':len(p._ACTIVE_REQUESTS)}
        rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    (OUT/'live-cancellation.json').write_text(json.dumps({'early_cancel_status':early['status'],'trials':rows},ensure_ascii=False,indent=2),encoding='utf-8')
finally:
    server.shutdown();server.server_close();thread.join()
