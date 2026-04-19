# Entry point — delegates to /app/app.py
import sys
if '/app' not in sys.path:
    sys.path.insert(0, '/app')
import app
app.run()
