"""Playwright screenshot test - produces visual images."""
import os
import sys
from playwright.sync_api import sync_playwright

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(PROJECT_ROOT)

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        # 1. Navigate to example.com
        page.goto("https://example.com", timeout=30000)
        title = page.title()
        print(f"Page title: {title}")

        # 2. Screenshot example.com
        path1 = os.path.join(PROJECT_ROOT, "screenshot_example_com.png")
        page.screenshot(path=path1, full_page=False)
        size1 = os.path.getsize(path1)
        print(f"screenshot_example_com.png -> {size1} bytes")

        # 3. Navigate to a more complex page (Wikipedia)
        page.goto("https://www.wikipedia.org", timeout=30000)
        title2 = page.title()
        print(f"Wikipedia title: {title2}")

        # 4. Screenshot Wikipedia
        path2 = os.path.join(PROJECT_ROOT, "screenshot_wikipedia.png")
        page.screenshot(path=path2, full_page=False)
        size2 = os.path.getsize(path2)
        print(f"screenshot_wikipedia.png -> {size2} bytes")

        # 5. Navigate to a local HTML file (if exists)
        local_path = os.path.join(PROJECT_ROOT, "index.html")
        if os.path.exists(local_path):
            page.goto(f"file://{local_path}", timeout=10000)
            path3 = os.path.join(PROJECT_ROOT, "screenshot_local.png")
            page.screenshot(path=path3, full_page=False)
            size3 = os.path.getsize(path3)
            print(f"screenshot_local.png -> {size3} bytes")

        browser.close()

    # Summary
    print("\n=== Screenshot Test Results ===")
    for path in [path1, path2]:
        if os.path.exists(path):
            name = os.path.basename(path)
            size = os.path.getsize(path)
            print(f"  {name}: {size} bytes - OK")
        else:
            name = os.path.basename(path)
            print(f"  {name}: MISSING - FAILED")

    # Verify files exist
    all_ok = all(os.path.exists(p) for p in [path1, path2])
    if all_ok:
        print("\nAll screenshots created successfully!")
    else:
        print("\nSome screenshots are missing.")
        sys.exit(1)

if __name__ == "__main__":
    run()
