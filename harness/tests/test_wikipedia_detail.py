"""Detailed analysis of the Wikipedia screenshot."""
import os
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)

JS_GET_TEXT = """
(function() {
    var els = document.querySelectorAll('*');
    var result = [];
    for (var i = 0; i < els.length && result.length < 200; i++) {
        var t = els[i].textContent.trim();
        if (t.length > 0) result.push(t);
    }
    return result.join(' ||| ');
})()
"""

JS_GET_LINKS = """
(function() {
    var links = document.querySelectorAll('a[href]');
    var result = [];
    for (var i = 0; i < links.length && result.length < 30; i++) {
        var t = links[i].textContent.trim();
        if (t.length > 0) result.push(t);
    }
    return result;
})()
"""

JS_GET_HEADINGS = """
(function() {
    var headings = document.querySelectorAll('h1, h2, h3');
    var result = [];
    for (var i = 0; i < headings.length && result.length < 20; i++) {
        var tag = headings[i].tagName;
        var text = headings[i].textContent.trim();
        if (text.length > 0) result.push(tag + ': ' + text);
    }
    return result;
})()
"""

JS_GET_INPUTS = """
(function() {
    var inputs = document.querySelectorAll('input');
    var result = [];
    for (var i = 0; i < inputs.length; i++) {
        result.push({
            type: inputs[i].type,
            name: inputs[i].name,
            placeholder: inputs[i].placeholder,
            id: inputs[i].id
        });
    }
    return result;
})()
"""


def safe_text(s):
    """Remove non-ASCII characters for console display."""
    if isinstance(s, str):
        return s.encode('ascii', 'ignore').decode('ascii')
    return s


def run():
    results = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        page.goto("https://www.wikipedia.org", timeout=30000)

        # Wait for any JS to finish
        page.wait_for_timeout(3000)

        # Get the full HTML body
        body_text = page.evaluate(JS_GET_TEXT)

        # Get all link texts
        links = page.evaluate(JS_GET_LINKS)

        # Get the h1/h2 tags
        headings = page.evaluate(JS_GET_HEADINGS)

        # Get all input elements (search box)
        inputs = page.evaluate(JS_GET_INPUTS)

        # Screenshot full page
        screenshot_path = os.path.join(ROOT, "screenshot_wikipedia_full.png")
        page.screenshot(path=screenshot_path, full_page=True)

        browser.close()

    results["body"] = body_text
    results["headings"] = headings
    results["links"] = links
    results["inputs"] = inputs
    results["screenshot_size"] = os.path.getsize(screenshot_path)

    # Write to file for inspection
    output_file = os.path.join(ROOT, "wikipedia_analysis.txt")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=== Page Content (first 1500 chars) ===\n")
        f.write(body_text[:1500])
        f.write("\n\n=== Headings ===\n")
        for h in headings:
            f.write(f"  {h}\n")
        f.write("\n=== Links (first 30) ===\n")
        for l in links:
            f.write(f"  - {safe_text(l)}\n")
        f.write("\n=== Inputs ===\n")
        for i in inputs:
            f.write(f"  {i}\n")
        f.write(f"\nFull-page screenshot: {results['screenshot_size']} bytes -- {'OK' if os.path.exists(screenshot_path) else 'MISSING'}\n")

    # Print to console (safe version)
    print("=== Page Content (first 1500 chars) ===")
    print(safe_text(body_text[:1500]))
    print("\n=== Headings ===")
    for h in headings:
        print(f"  {safe_text(h)}")
    print("\n=== Links (first 30) ===")
    for l in links:
        print(f"  - {safe_text(l)}")
    print("\n=== Inputs ===")
    for i in inputs:
        print(f"  {safe_text(str(i))}")

    print(f"\nFull-page screenshot: {results['screenshot_size']} bytes -- {'OK' if os.path.exists(screenshot_path) else 'MISSING'}")
    print(f"Analysis written to: {output_file}")

if __name__ == "__main__":
    run()
