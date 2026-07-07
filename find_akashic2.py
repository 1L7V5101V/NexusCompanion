import os

for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__', 'node_modules', '.venv', 'static', '.NexusCompanion.ObsidianNotes', 'package-lock.json']]
    for f in files:
        if f.endswith(('.py', '.toml', '.yml', '.yaml', '.json', '.md', '.txt', '.js', '.ts', '.css', '.html')):
            p = os.path.join(root, f)
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as fp:
                    c = fp.read()
                if 'akashic' in c.lower():
                    print(p)
            except:
                pass