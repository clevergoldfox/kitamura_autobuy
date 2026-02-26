# URL Detection Tool

This tool opens the browser at startup on:

`https://shop.kitamura.jp/ec/used/2442970035329`

When the page URL changes and includes:

`https://shop.kitamura.jp/ec/used/`

it shows a notification:

`The page has changed.`

## Requirements

- Python 3.8+
- PyQt5
- Playwright (Chromium/Edge/Chrome)

## Setup

```bash
cd url_detection
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python main.py
```
