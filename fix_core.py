import re

f = r'plugins/rachael/core.py'

with open(f, 'r', encoding='utf-8') as fp:
    content = fp.read()

content = content.replace('plugins.akasha', 'plugins.rachael')
content = re.sub(r'(?<![a-z])Akasha(?=[A-Z])', 'Rachael', content)
content = re.sub(r'(?<![a-zA-Z_])akasha_(?!node|edge|query_log|activation_event|embedding_cache|salience_state|migration_run|source_session|fts_)', 'rachael_', content)
content = re.sub(r'(?<![a-zA-Z_])akasha(?![a-zA-Z_])', 'rachael', content)
content = re.sub(r'(?<![a-zA-Z_])Akasha(?![a-zA-Z_])', 'Rachael', content)
content = content.replace('name = "Rachael"', 'name = "rachael"')
content = content.replace('name="Rachael"', 'name="rachael"')
content = content.replace('engine_kind="Rachael"', 'engine_kind="rachael"')
content = content.replace('"lane": "Rachael"', '"lane": "rachael"')
content = content.replace('plugin_id = "Rachael"', 'plugin_id = "rachael"')
content = content.replace('"Rachaellast"', '"rachaellast"')
content = content.replace('"/Rachaellast"', '"/rachaellast"')
content = content.replace('/api/dashboard/Rachael-', '/api/dashboard/rachael-')
content = content.replace('/api/dashboard/akasha-', '/api/dashboard/rachael-')
content = content.replace('akasha.db', 'rachael.db')
content = content.replace('akasha_graph_snapshot.json', 'rachael_graph_snapshot.json')
content = content.replace('akasha.last_query', 'rachael.last_query')
content = content.replace('is_memory_engine(*, "Rachael")', 'is_memory_engine(*, "rachael")')

with open(f, 'w', encoding='utf-8') as fp:
    fp.write(content)
print(f'{f}: done')