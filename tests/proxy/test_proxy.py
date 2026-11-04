import concurrent.futures, importlib.util, json, pathlib, threading, time
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('optimized_proxy_tests',ROOT.parent.parent/'ollama_proxy.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);p.logf.close();p.log=lambda msg:None

def reset():
    p._PIECE_CACHE=p.LruCache(1000);p._RESULT_CACHE.clear()

def request(texts,source='Japanese',model='model'):
    data={'source_language':source,'target_language':'Chinese (Simplified)','text_blocks':[{'id':str(i),'text':t} for i,t in enumerate(texts)]}
    return json.dumps({'model':model,'prompt':json.dumps(data,ensure_ascii=False)},ensure_ascii=False).encode()

# Regression: shared kanji must not bypass the model; English UI labels in Japanese mode still pass through.
seen=[]
def translate(text,*args,**kwargs):
    seen.append(text)
    return '||'.join('中文译文'+str(i) for i,_ in enumerate(text.split('||')))
with patch.object(p,'translate_native',side_effect=translate):
    result=p.handle_generate_request(request(['体力回復','体力回復薬','MENU']))
    assert seen==['体力回復||体力回復薬'],seen
    assert result['text_blocks'][2]['text']=='MENU'
    assert not p.needs_translation('设置')
    assert p.needs_translation('体力回復')
    p.handle_generate_request(request(['体力回復'],source='English'))
    assert len(seen)==2,'source languages incorrectly shared a cached result'

# LRU and frames larger than its capacity: response reconstruction must not lose evicted entries.
reset()
with patch.object(p,'translate_native',side_effect=translate):
    result=p.handle_generate_request(request(['第'+str(i)+'番です' for i in range(1025)]))
    assert len(result['text_blocks'])==1025 and len(p._PIECE_CACHE)==1000
    assert result['text_blocks'][0]['id']=='0' and result['text_blocks'][-1]['id']=='1024'
    assert ('第1024番です','简体中文','model','ja') in p._PIECE_CACHE
    assert ('第0番です','简体中文','model','ja') not in p._PIECE_CACHE
    # A cache hit moves the entry to the front, and must survive the next insertion.
    p.handle_generate_request(request(['第25番です']))
    p.handle_generate_request(request(['追加です']))
    assert ('第25番です','简体中文','model','ja') in p._PIECE_CACHE
    assert ('第26番です','简体中文','model','ja') not in p._PIECE_CACHE

# A malformed multi-piece output must split; no ambiguous assignment to IDs.
reset();calls=[]
def malformed(text,*args,**kwargs):
    calls.append(text)
    return '中文一段' if '||' in text else ('你好' if text=='こんにちは' else '晚安')
with patch.object(p,'translate_native',side_effect=malformed):
    r=p.handle_generate_request(request(['こんにちは','こんばんは']))
    assert [b['text'] for b in r['text_blocks']]==['你好','晚安'] and len(calls)==3

# Repair only the bad part, once, and do not poison the cache if correction fails.
reset();calls=[]
def repair(text,*args,**kwargs):
    calls.append((text,kwargs))
    if '||' in text:return '体力が減ったら回復薬を使ってください。||请打开门。'
    return '体力减少时，请使用恢复药。'
with patch.object(p,'translate_native',side_effect=repair):
    r=p.handle_generate_request(request(['体力が減ったら回復薬を使いましょう。','扉を開けてください。']))
    assert len(calls)==2 and calls[1][1].get('correction') and len(p._PIECE_CACHE)==2
reset();calls=[]
def no_repair(text,*args,**kwargs):
    calls.append(1);return '回復薬を使ってください。'
with patch.object(p,'translate_native',side_effect=no_repair):
    try:p.handle_generate_request(request(['回復薬を使いましょう。']));raise AssertionError('accepted Japanese')
    except p.OllamaGenerationError:pass
    assert len(calls)==2 and not p._PIECE_CACHE and not p._RESULT_CACHE
reset();calls=[]
def protect(text,*args,**kwargs):
    calls.append(1);return '{player}获得药品。' if kwargs.get('correction') else '获得药品。'
with patch.object(p,'translate_native',side_effect=protect):
    assert p.handle_generate_request(request(['{player}は薬を手に入れた。']))['text_blocks'][0]['text']=='{player}获得药品。'
    assert len(calls)==2

reset()
def repair_literal_pipe(text,*args,**kwargs):
    return '请使用条件A|B。' if kwargs.get('correction') else '条件A|Bを使ってください。'
with patch.object(p,'translate_native',side_effect=repair_literal_pipe):
    assert p.handle_generate_request(request(['条件A|Bを使いましょう。']))['text_blocks'][0]['text']=='请使用条件A|B。'

# Duplicate concurrent requests still perform exactly one generation; thread-local source restores correctly.
reset();seen.clear()
def delayed(text,*args,**kwargs):time.sleep(.03);seen.append(text);return '你好'
with patch.object(p,'translate_native',side_effect=delayed):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(p.handle_generate_request,[request(['こんにちは'])]*8))
    assert len(seen)==1 and all(r==result[0] for r in result)

# Cancelled queued work must never acquire GPU, and cancellation-before-arrival is remembered.
request_id='unit-cancel-before'
p.cancel_request(request_id)
data=json.loads(request(['こんにちは']));data['_rst']={'request_id':request_id}
try:p.handle_generate_request(json.dumps(data).encode());raise AssertionError('accepted cancelled request')
except p.TranslationCancelled:pass
assert not p._ACTIVE_REQUESTS

print('PASS: source detection/isolation, LRU + >capacity frames, ID-safe fallback, bounded per-piece correction, protected markers, no invalid cache, concurrent deduplication, cancellation tombstones')
