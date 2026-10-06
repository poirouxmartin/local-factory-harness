"""Playwright screenshot + content verification."""
import os
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        # Page 1: example.com
        page.goto("https://example.com", timeout=30000)
        title = page.title()
        h1 = page.query_selector("h1").inner_text() if page.query_selector("h1") else "NO H1"
        para = page.query_selector("p").inner_text() if page.query_selector("p") else "NO P"
        page.screenshot(path=os.path.join(ROOT, "screenshot_example.png"), full_page=False)
        print(f"[example.com] title={title} | h1={h1} | p={para}")

        # Page 2: Wikipedia
        page.goto("https://www.wikipedia.org", timeout=30000)
        title2 = page.title()
        search_box = page.query_selector("#searchInput")
        search_val = search_box.get_attribute("placeholder") if search_box else "NO SEARCH BOX"
        main_heading = page.query_selector("#main-message h1")
        main_text = main_heading.inner_text() if main_heading else "NO H1"
        lang_links = page.query_selector_all("#language a")
        lang_count = len(lang_links)
        top_links = [a.inner_text() for a in lang_links[:5]]
        page.screenshot(path=os.path.join(ROOT, "screenshot_wikipedia.png"), full_page=False)
        print(f"[Wikipedia] title={title2} | h1={main_text} | search_placeholder={search_val}")
        print(f"  Languages available: {lang_count}, first 5: {top_links}")

        # Page 3: a local HTML page (more controlled test)
        html_path = os.path.join(ROOT, "screenshot_local.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("""<!DOCTYPE html>
<html>
<head><title>Playwright Test Page</title>
<style>
body { font-family: sans-serif; padding: 40px; background: #f5f5f5; }
.card { background: white; border-radius: 8px; padding: 24px; margin: 16px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
h1 { color: #333; }
button { background: #0066cc; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; }
.status { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; margin: 4px; }
.ok { background: #d4edda; color: #155724; }
.fail { background: #f8d7da; color: #721c24; }
</style>
</head>
<body>
<h1>Playwright Screenshot Test</h1>
<div class="card">
  <h2>Test Results</h2>
  <span class="status ok">Browser: Chromium</span>
  <span class="status ok">Headless: true</span>
  <span class="status ok">Viewport: 1280x720</span>
  <span class="status ok">JavaScript: enabled</span>
  <p>This page verifies that screenshots render correctly.</p>
  <button onclick="alert('Button works!')">Click me</button>
</div>
<div class="card">
  <h2>Color Palette Test</h2>
  <div style="display: flex; gap: 8px;">
    <div style="width:60px;height:60px;background:#ff6b6b;border-radius:4px;"></div>
    <div style="width:60px;height:60px;background:#4ecdc4;border-radius:4px;"></div>
    <div style="width:60px;height:60px;background:#45b7d1;border-radius:4px;"></div>
    <div style="width:60px;height:60px;background:#96ceb4;border-radius:4px;"></div>
    <div style="width:60px;height:60px;background:#ffeaa7;border-radius:4px;"></div>
  </div>
</div>
</body>
</html>""")

        page.goto(f"file://{html_path}", timeout=10000)
        title3 = page.title()
        btn_text = page.query_selector("button").inner_text() if page.query_selector("button") else "NO BTN"
        page.screenshot(path=os.path.join(ROOT, "screenshot_local.png"), full_page=False)
        print(f"[Local HTML] title={title3} | button={btn_text}")

        browser.close()

    # Summary
    print("\n=== Files ===")
    for f in ["screenshot_example.png", "screenshot_wikipedia.png", "screenshot_local.png"]:
        fp = os.path.join(ROOT, f)
        if os.path.exists(fp):
            print(f"  {f}: {os.path.getsize(fp)} bytes -- OK")
        else:
            print(f"  {f}: MISSING -- FAILED")

if __name__ == "__main__":
    run()
