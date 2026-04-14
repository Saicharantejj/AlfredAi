import os

dashboard_path = 'static/dashboard.html'
engine_path = 'static/shortcut-engine.js'
output_path = 'static/standalone_dashboard.html'

with open(dashboard_path, 'r') as f:
    dashboard_html = f.read()

with open(engine_path, 'r') as f:
    engine_js = f.read()

# Replace script tag
script_tag = '<script src="/static/shortcut-engine.js"></script>'
replacement = f'<script>\n{engine_js}\n</script>'
standalone_html = dashboard_html.replace(script_tag, replacement)

# Replace default name
standalone_html = standalone_html.replace('User.', 'SAICHARAN.')
standalone_html = standalone_html.replace("localStorage.getItem('alfred_user')", "localStorage.getItem('alfred_user') || 'SAICHARAN'")

with open(output_path, 'w') as f:
    f.write(standalone_html)

print(f"Standalone dashboard created at {output_path}")
