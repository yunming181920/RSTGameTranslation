import importlib.util,pathlib
from unittest.mock import patch
s=importlib.util.spec_from_file_location('proxy',pathlib.Path(__file__).resolve().parents[2]/'ollama_proxy.py');p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
p.logf.close();p.log=lambda message:None
seen=[]
def model(payload):
    seen.append(payload)
    body=payload['prompt'].split('\n\n',1)[1]
    translations={'こんにちは':'你好','こんばんは':'晚安','条件A||Bです':'条件A||B','A|Bです':'A|B'}
    if '以下共' in payload['prompt']:
        answer='||'.join(translations[t] for t in body.split('||'))
    else:answer=translations[body]
    return {'response':answer,'done_reason':'stop'}
with patch.object(p,'call_ollama_generate',side_effect=model):
    blocks=[{'id':'b7','text':'こんにちは'+p.SEP+'MENU'+p.SEP+'こんばんは'},{'id':'b1','text':'こんにちは'}]
    r=p._translate_blocks(blocks,'简体中文','model')['text_blocks']
    assert len(seen)==1 and seen[0]['prompt'].split('\n\n',1)[1]=='こんにちは||こんばんは'
    assert '共2段' in seen[0]['prompt'] and p.SEP not in seen[0]['prompt']
    assert r==[{'id':'b7','text':'你好'+p.SEP+'MENU'+p.SEP+'晚安'},{'id':'b1','text':'你好'}]
    p._PIECE_CACHE.clear();seen.clear()
    blocks=[{'id':'a','text':'こんにちは'},{'id':'pipe','text':'条件A||Bです'},{'id':'single-pipe','text':'A|Bです'},{'id':'b','text':'こんばんは'}]
    r=p._translate_blocks(blocks,'简体中文','model')['text_blocks']
    assert [b['text'] for b in r]==['你好','条件A||B','A|B','晚安']
    literal=[q for q in seen if q['prompt'].split('\n\n',1)[1] in ['条件A||Bです','A|Bです']]
    assert len(literal)==2 and all('以下共' not in q['prompt'] for q in literal)
for bad in ['你好|晚安','你好|||晚安','你好##|||##晚安','你好||','你好｜｜晚安']:
    p._PIECE_CACHE.clear();seen.clear()
    def malformed(payload):
        if '以下共' in payload['prompt']:
            seen.append(payload);return {'response':bad,'done_reason':'stop'}
        return model(payload)
    with patch.object(p,'call_ollama_generate',side_effect=malformed):
        r=p._translate_blocks([{'id':'a','text':'こんにちは'},{'id':'b','text':'こんばんは'}],'简体中文','model')['text_blocks']
        assert r==[{'id':'a','text':'你好'},{'id':'b','text':'晚安'}] and len(seen)==3,(bad,r,seen)
print('PASS: model prompt uses ||; segment count/order; RST framing/IDs; literal pipes; malformed separators split safely')
