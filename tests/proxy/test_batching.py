import importlib.util,pathlib
from unittest.mock import patch
s=importlib.util.spec_from_file_location('proxy',pathlib.Path(__file__).resolve().parents[2]/'ollama_proxy.py');p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
p.logf.close();p.log=lambda message:None
seen=[]
def render(text):return '译文'+text.encode('utf-8').hex()
def translate(text,target,model,**kwargs):
    seen.append(text);return p.MODEL_SEP.join(render(piece) for piece in text.split(p.MODEL_SEP))
def run(blocks):
    p._PIECE_CACHE.clear();seen.clear();return p._translate_blocks(blocks,'简体中文','model')
with patch.object(p,'translate_native',side_effect=translate):
    blocks=[{'id':'nine','text':'こんにちは'+p.SEP+'MENU'+p.SEP+'こんばんは'},{'id':'two','text':'こんにちは'},{'id':'three','text':'设置'}]
    result=run(blocks)['text_blocks'];assert [b['id'] for b in result]==['nine','two','three']
    assert result[0]['text']==render('こんにちは')+p.SEP+'MENU'+p.SEP+render('こんばんは')
    assert result[1]['text']==render('こんにちは') and result[2]['text']=='设置'
    assert len(seen)==1 and len(seen[0].split(p.MODEL_SEP))==2
    result=run([{'id':str(i),'text':'こんにちは'+str(i)} for i in range(25)])
    assert [len(t.split(p.MODEL_SEP)) for t in seen]==[12,12,1]
    assert all(b['text']==render('こんにちは'+str(i)) for i,b in enumerate(result['text_blocks']))
    long=[{'id':str(i),'text':'あ'*398+str(i)} for i in range(5)]
    run(long);assert all(len(t)<=900 for t in seen)
    p._PIECE_CACHE=p.LruCache(1000)
    for i in range(1001):p._PIECE_CACHE[('こんにちは'+str(i),'简体中文','model')]='cached'
    result=p._translate_blocks([{'id':'old','text':'こんにちは1'},{'id':'new','text':'こんばんは'}],'简体中文','model')
    assert result['text_blocks'][0]['text']=='cached' and result['text_blocks'][1]['text']==render('こんばんは')
    assert len(p._PIECE_CACHE)==1000
# Incorrect separator count must split, never assign an ambiguous response to IDs.
seen.clear();p._PIECE_CACHE.clear()
def mismatch(text,*args,**kwargs):
    seen.append(text)
    return 'bad merged output' if p.MODEL_SEP in text else render(text)
with patch.object(p,'translate_native',side_effect=mismatch):
    result=p._translate_blocks([{'id':'a','text':'こんにちは'},{'id':'b','text':'こんばんは'}],'简体中文','model')
    assert len(seen)==3 and [b['text'] for b in result['text_blocks']]==[render('こんにちは'),render('こんばんは')]
p._PIECE_CACHE.clear()
def fail_one(text,*args,**kwargs):
    if '危険' in text:raise p.OllamaGenerationError('token repeat limit reached')
    return render(text)
with patch.object(p,'translate_native',side_effect=fail_one):
    try:p._translate_blocks([{'id':'a','text':'こんにちは'},{'id':'b','text':'危険'}],'简体中文','model');raise AssertionError('accepted failed group')
    except p.OllamaGenerationError:pass
    assert ('危険','简体中文','model') not in p._PIECE_CACHE
    assert ('こんにちは','简体中文','model') in p._PIECE_CACHE
print('PASS: cross-block batching, IDs/order, separators, unchanged text, duplicate reuse, piece/char bounds, cache eviction, fallback splitting, failed-result exclusion')

