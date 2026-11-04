import importlib.util, json, pathlib, time, uuid

ROOT=pathlib.Path(__file__).resolve().parent
OUT=ROOT.parent/'artifacts';OUT.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('verified_proxy',ROOT.parent.parent/'ollama_proxy.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);p.logf.close();logs=[];p.log=logs.append
fixtures=json.loads((ROOT.parent/'fixtures/prompt-stability.json').read_text(encoding='utf-8'))
cases=[([case],case['id']) for case in fixtures['holdout']]
cases += [([{'id':f'kanji{i}','text':text}],f'kanji{i}') for i,text in enumerate(['体力回復','体力回復薬','装備変更'])]
cases += [(fixtures['holdout'][:n],f'batch{n}') for n in [2,4,12]]
rows=[]
for blocks,key in cases:
    p._PIECE_CACHE.clear();p._RESULT_CACHE.clear();logs.clear()
    payload={'model':'index-translate-2b','_rst':{'request_id':uuid.uuid4().hex},'prompt':json.dumps({'source_language':'Japanese','target_language':'Chinese (Simplified)','text_blocks':[{'id':b['id'],'text':b['text']} for b in blocks]},ensure_ascii=False)}
    start=time.perf_counter();result=p.handle_generate_request(json.dumps(payload,ensure_ascii=False).encode());ms=(time.perf_counter()-start)*1000
    out=result['text_blocks']
    assert [b['id'] for b in out]==[b['id'] for b in blocks]
    assert all(b['text'] and not any('\u3041'<=c<='\u3096' for c in b['text']) for b in out),(key,result)
    row={'key':key,'wall_ms':ms,'inputs':blocks,'result':result,'logs':list(logs)};rows.append(row)
    (OUT/'verified-translations.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'requests':len(rows),'segments':sum(len(r['inputs']) for r in rows),'corrections':sum('translation correction' in s for r in rows for s in r['logs']),'kanji':{r['key']:r['result'] for r in rows if r['key'].startswith('kanji')}},ensure_ascii=False,indent=2))
