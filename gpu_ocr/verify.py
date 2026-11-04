import json
from backend import Backend

if __name__ == '__main__':
    backend = Backend('dml')
    print(json.dumps(backend.info(), ensure_ascii=False, indent=2))
