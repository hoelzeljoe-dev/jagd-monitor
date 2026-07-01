x#!/usr/bin/env python3
"""
Jagdgenossenschaft-Monitor – Autonomer Wochenscan
Scannt Wittich-Mitteilungsblätter + Gemeinde-PDFs (Breitengüßbach, Hirschaid)
auf Jagdgenossenschafts-Einträge und schickt eine E-Mail bei neuen Treffern.
"""

import json
import os
import re
import io
import hashlib
import smtplib
import datetime
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
import pdfplumber

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

KEYWORDS = [
    'jagdgenossenschaft', 'jagdversammlung', 'jagdverpachtung', 'jagdpacht',
    'wildschaden', 'hegering', 'jagdvorstand', 'jagdbeirat', 'jagdpächter',
    'jagdgenossen', 'jagdrevier', 'jagdessen', 'jagdvorsteher', 'jagdschilling',
    'rehessen', 'jagdpachtschilling', 'jagdpächtern', 'jagdvorstandschaft',
]

WITTICH_PUBS = {
    '2010': {'name': 'Gemeinde Bischberg',         'plz': '96120'},
    '2136': {'name': 'Markt Zapfendorf',            'plz': '96199'},
    '2108': {'name': 'Gemeinde Viereth-Trunstadt',  'plz': '96191'},
    '2342': {'name': 'Gemeinde Frensdorf',          'plz': '96173'},
    '2050': {'name': 'Markt Heiligenstadt i.OFr.',  'plz': '91332'},
    '2147': {'name': 'Stadt Zeil a. Main',          'plz': '97514'},
    '2100': {'name': 'Gemeindeblatt Strullendorf',  'plz': '96173'},
    '2082': {'name': 'VG Ebern',                    'plz': '97514'},
}

STATE_FILE    = 'known_findings.json'
FINDINGS_FILE = 'findings_log.json'
YEAR = datetime.date.today().year
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; JagdMonitor/1.0)'})


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def has_keyword(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in KEYWORDS)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'last_scan': None,
        'last_ausgabe': {},
        'last_pdf_issue': {},
        'known_pdf_keys': [],
    }


def save_state(state: dict):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_findings_log() -> dict:
    if os.path.exists(FINDINGS_FILE):
        with open(FINDINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'last_scan': None, 'last_kw': None, 'findings': []}


def save_findings_log(new_findings: list):
    """Hängt neue Treffer an findings_log.json an (max. 500 Einträge gesamt)."""
    log_data = load_findings_log()
    today    = datetime.date.today()
    kw       = today.isocalendar()[1]
    year     = today.year
    for f in new_findings:
        entry = {
            'date_found': today.isoformat(),
            'kw':         kw,
            'year':       year,
            'source':     f.get('source', ''),
            'plz':        f.get('plz', ''),
            'ausgabe':    f.get('ausgabe', ''),
            'title':      f.get('title', ''),
            'url':        f.get('url', ''),
            'excerpt':    f.get('excerpt', ''),
        }
        log_data['findings'].append(entry)
    # Neueste zuerst, max 500
    log_data['findings'] = log_data['findings'][-500:]
    log_data['last_scan'] = datetime.datetime.now().isoformat()
    log_data['last_kw']   = kw
    log_data['last_year'] = year
    with open(FINDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)


def make_pdf_key(source: str, issue: str, text: str) -> str:
    raw = f"{source}|{issue}|{text[:120]}"
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def fetch(url: str, timeout: int = 20, retries: int = 3):
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log(f"  FEHLER {url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Wittich-Scanner
# ---------------------------------------------------------------------------

def wittich_list_ausgabe(titel_id: str, ausgabe: int, year: int = YEAR) -> list:
    """Gibt alle Artikel einer Ausgabe zurück (alle Seiten, max 20/Seite)."""
    articles = []
    page = 0
    while True:
        url = (
            f"https://ol.wittich.de/titel/{titel_id}/ausgabe/{ausgabe}/{year}"
            f"/rubrik/alle/seite/{page}/sortiert/inhaltsverzeichnis"
        )
        resp = fetch(url)
        if not resp:
            break
        soup = BeautifulSoup(resp.text, 'html.parser')
        links = soup.select('a[href*="/artikel/"]')
        if not links:
            break
        for a in links:
            href = a.get('href', '')
            if not href:
                continue
            full_url = ('https://ol.wittich.de' + href) if href.startswith('/') else href
            if not any(x['url'] == full_url for x in articles):
                container = a.find_parent(['li', 'div', 'article']) or a
                articles.append({
                    'url': full_url,
                    'title': a.get_text(strip=True),
                    'excerpt': container.get_text(' ', strip=True),
                })
        if len(links) < 20:
            break
        page += 1
        time.sleep(0.3)
    return articles


def wittich_article_text(url: str) -> str:
    resp = fetch(url)
    if not resp:
        return ''
    soup = BeautifulSoup(resp.text, 'html.parser')
    for sel in ['.artikel-inhalt', '.article-content', '.content-text', 'article', 'main']:
        el = soup.select_one(sel)
        if el:
            return el.get_text(' ', strip=True)
    return soup.get_text(' ', strip=True)[:3000]


def best_excerpt(text: str, max_len: int = 400) -> str:
    for p in re.split(r'\n{2,}', text):
        if has_keyword(p):
            return p.strip()[:max_len]
    return text[:max_len]


def scan_wittich(state: dict) -> list:
    findings = []
    last_ausgabe = state.setdefault('last_ausgabe', {})
    current_kw = datetime.date.today().isocalendar()[1]

    for titel_id, pub in WITTICH_PUBS.items():
        start = (last_ausgabe.get(titel_id) or 0) + 1
        # Bischberg nutzt KW-Nummern; sequenzielle Pubs: max 20 voraus
        end = current_kw + 1 if titel_id == '2010' else start + 20
        max_empty = 4
        consecutive_empty = 0
        highest_found = last_ausgabe.get(titel_id) or 0

        log(f"  {pub['name']}: prüfe Ausgaben {start}–{end - 1}")

        for ausgabe_nr in range(start, end):
            articles = wittich_list_ausgabe(titel_id, ausgabe_nr)
            if not articles:
                consecutive_empty += 1
                if consecutive_empty >= max_empty:
                    break
                continue

            consecutive_empty = 0
            highest_found = ausgabe_nr

            for art in articles:
                if not has_keyword(art['title']) and not has_keyword(art['excerpt']):
                    continue
                full_text = wittich_article_text(art['url'])
                if not has_keyword(full_text):
                    continue
                findings.append({
                    'source': pub['name'],
                    'plz': pub['plz'],
                    'ausgabe': f"Ausgabe {ausgabe_nr}/{YEAR}",
                    'title': art['title'],
                    'url': art['url'],
                    'excerpt': best_excerpt(full_text),
                })
                log(f"    ✓ {art['title'][:80]}")
            time.sleep(0.5)

        if highest_found > (last_ausgabe.get(titel_id) or 0):
            last_ausgabe[titel_id] = highest_found

    return findings


# ---------------------------------------------------------------------------
# PDF-Scanner (gemeinsam für Breitengüßbach + Hirschaid)
# ---------------------------------------------------------------------------

def pdf_text(url: str):
    resp = fetch(url, timeout=40)
    if not resp:
        return None
    try:
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except Exception as e:
        log(f"  PDF-Fehler {url}: {e}")
        return None


def extract_jagd_sections(text: str, source: str, issue_key: str,
                           url: str, known_keys: set) -> list:
    results = []
    chunks = re.split(r'\n{2,}', text)
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        if has_keyword(chunk):
            ctx_parts = chunks[max(0, i - 1): min(len(chunks), i + 4)]
            context = '\n'.join(ctx_parts).strip()
            key = make_pdf_key(source, issue_key, context)
            if key not in known_keys:
                title_line = next(
                    (ln.strip() for ln in context.splitlines() if ln.strip()), context[:60]
                )
                results.append({
                    'source': source,
                    'ausgabe': issue_key,
                    'title': title_line[:120],
                    'url': url,
                    'excerpt': context[:500],
                    '_key': key,
                })
                known_keys.add(key)
            i += 4
        else:
            i += 1
    return results


# ---------------------------------------------------------------------------
# Breitengüßbach
# ---------------------------------------------------------------------------

BGBACH_MONTHS = [
    ('01', 'Januar'), ('02', 'Februar'), ('03', 'M%C3%A4rz'),
    ('04', 'April'),  ('05', 'Mai'),     ('06', 'Juni'),
    ('07', 'Juli'),   ('08', 'August'),  ('09', 'September'),
    ('10', 'Oktober'),('11', 'November'),('12', 'Dezember'),
]


def scan_breitenguessbach(state: dict) -> list:
    findings = []
    known_keys = set(state.get('known_pdf_keys', []))
    last = state.setdefault('last_pdf_issue', {}).get('breitenguessbach', f'{YEAR}-05')
    last_idx = next((i for i, (m, _) in enumerate(BGBACH_MONTHS)
                     if f'{YEAR}-{m}' == last), 4)

    for idx in range(last_idx + 1, len(BGBACH_MONTHS)):
        mm, name = BGBACH_MONTHS[idx]
        url = (
            f"https://www.breitenguessbach.de/fileadmin/Gemeinde/Buergerservice_und_Politik"
            f"/Buergerservice/Mitteilungsblatt/{YEAR}/{YEAR}_{mm}_MBL_{name}.pdf"
        )
        issue_key = f"MBL {name} {YEAR}"
        log(f"  Breitengüßbach {name} {YEAR} …")
        text = pdf_text(url)
        if not text:
            break
        secs = extract_jagd_sections(text, 'Gemeinde Breitengüßbach', issue_key, url, known_keys)
        for s in secs:
            log(f"    ✓ {s['title'][:80]}")
        findings.extend(secs)
        state['last_pdf_issue']['breitenguessbach'] = f'{YEAR}-{mm}'

    state['known_pdf_keys'] = list(known_keys)
    return findings


# ---------------------------------------------------------------------------
# Hirschaid
# ---------------------------------------------------------------------------

# CMS-Eigenheit: Ordnernamen entsprechen NICHT dem Erscheinungsdatum.
# Bei neuen Ausgaben (Nr. 27+) wird zunächst die Indexseite geprüft.
HIRSCHAID_FOLDERS = [
    f'{YEAR - 1}/november',
    f'{YEAR}/maerz',
    f'{YEAR}/juni',
    f'{YEAR}/september',
    f'{YEAR}/dezember',
]
HIRSCHAID_INDEX = f'https://www.hirschaid.de/seite/de/markt/11109/-/{YEAR}.html'


def hirschaid_folders_from_index() -> list:
    """Ermittelt aktuelle Ordner aus der Hirschaid-Indexseite."""
    resp = fetch(HIRSCHAID_INDEX, timeout=15, retries=2)
    if not resp:
        return HIRSCHAID_FOLDERS
    soup = BeautifulSoup(resp.text, 'html.parser')
    folders = set(HIRSCHAID_FOLDERS)
    for a in soup.select('a[href*="eigene_dateien/aktuell/"]'):
        href = a.get('href', '')
        m = re.search(r'aktuell/([^/]+/[^/]+)/', href)
        if m:
            folders.add(m.group(1))
    return list(folders)


def hirschaid_url(nr: int, folders: list, year: int = YEAR):
    for folder in folders:
        url = f"https://www.hirschaid.de/eigene_dateien/aktuell/{folder}/{nr}_{year}.pdf"
        resp = fetch(url, timeout=10, retries=1)
        if resp and len(resp.content) > 5000:
            return url
    return None


def scan_hirschaid(state: dict) -> list:
    findings = []
    known_keys = set(state.get('known_pdf_keys', []))
    last_nr = state.setdefault('last_pdf_issue', {}).get('hirschaid', 26)
    folders = hirschaid_folders_from_index()

    max_empty = 3
    empty = 0
    nr = last_nr + 1

    while empty < max_empty:
        url = hirschaid_url(nr, folders)
        if not url:
            empty += 1
            nr += 1
            continue
        empty = 0
        log(f"  Hirschaid Nr. {nr}/{YEAR} …")
        issue_key = f"Nr. {nr}/{YEAR}"
        text = pdf_text(url)
        if text:
            secs = extract_jagd_sections(text, 'Markt Hirschaid', issue_key, url, known_keys)
            for s in secs:
                log(f"    ✓ {s['title'][:80]}")
            findings.extend(secs)
        state['last_pdf_issue']['hirschaid'] = nr
        nr += 1
        time.sleep(1)

    state['known_pdf_keys'] = list(known_keys)
    return findings


# ---------------------------------------------------------------------------
# E-Mail
# ---------------------------------------------------------------------------

def send_email(findings: list):
    gmail_user = os.environ['GMAIL_USER']
    gmail_pass = os.environ['GMAIL_APP_PASSWORD']
    email_to   = os.environ.get('EMAIL_TO', gmail_user)
    email_cc   = os.environ.get('EMAIL_CC', '')

    kw   = datetime.date.today().isocalendar()[1]
    year = datetime.date.today().year

    if findings:
        subject = f"\U0001f98c Jagd-Monitor KW {kw}/{year}: {len(findings)} neue Treffer"
        body = _html_findings(findings, kw, year)
    else:
        subject = f"\U0001f98c Jagd-Monitor KW {kw}/{year}: Keine neuen Einträge"
        body = (
            f"<html><body style='font-family:Arial,sans-serif;max-width:700px'>"
            f"<h2 style='color:#1a3a1a'>\U0001f98c Jagdgenossenschaft-Monitor – KW {kw}/{year}</h2>"
            f"<p>Scan durchgeführt — keine neuen Jagdgenossenschafts-Einträge gefunden.</p>"
            f"<p style='color:#aaa;font-size:12px'>Nächster Scan: Montag KW {kw+1}</p>"
            f"</body></html>"
        )

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = gmail_user
    msg['To']      = email_to
    if email_cc:
        msg['Cc'] = email_cc
    msg.attach(MIMEText(body, 'html', 'utf-8'))

    recipients = [email_to] + ([email_cc] if email_cc else [])
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
        srv.login(gmail_user, gmail_pass)
        srv.sendmail(gmail_user, recipients, msg.as_string())
    log(f"E-Mail gesendet: {subject}")


def _html_findings(findings: list, kw: int, year: int) -> str:
    rows = ''
    for i, f in enumerate(findings, 1):
        plz_tag = (f'<span style="background:#e8f4e8;color:#2a6a2a;padding:1px 5px;'
                   f'border-radius:3px;font-size:10px;font-weight:700">{f["plz"]}</span>'
                   if f.get('plz') else '')
        rows += (
            f'<div style="margin-bottom:20px;padding:14px;border-left:4px solid #2a6a2a;'
            f'background:#f8faf8;border-radius:0 6px 6px 0">'
            f'<div style="font-size:11px;color:#888;margin-bottom:4px">'
            f'{i}. &nbsp;\U0001f4f0 <strong>{f["source"]}</strong>'
            f'&nbsp;&middot;&nbsp;{f.get("ausgabe","")}'
            f'&nbsp;&middot;&nbsp;{plz_tag}</div>'
            f'<div style="font-weight:700;color:#1a3a1a;font-size:14px;margin-bottom:6px">{f["title"]}</div>'
            f'<div style="font-size:12px;color:#555;line-height:1.5;white-space:pre-wrap">{f.get("excerpt","")}</div>'
            f'<a href="{f["url"]}" style="display:inline-block;margin-top:8px;font-size:12px;color:#2a6a2a">'
            f'→ Zum Artikel / PDF</a></div>'
        )
    return (
        f'<html><body style="font-family:Arial,sans-serif;color:#1a1a1a;max-width:700px;margin:0 auto">'
        f'<h2 style="color:#1a3a1a;border-bottom:2px solid #d0e8d0;padding-bottom:8px">'
        f'\U0001f98c Jagdgenossenschaft-Monitor – KW {kw}/{year}</h2>'
        f'<p><strong>{len(findings)} neue Einträge</strong> gefunden:</p>'
        f'{rows}'
        f'<p style="color:#aaa;font-size:11px;border-top:1px solid #eee;margin-top:24px;padding-top:12px">'
        f'Automatischer Scan · Nächster Lauf: Montag KW {kw+1}/{year}</p>'
        f'</body></html>'
    )


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    log("=== Jagdgenossenschaft-Monitor gestartet ===")
    state = load_state()
    all_findings = []

    log("\n[1/3] Wittich-Mitteilungsblätter …")
    all_findings.extend(scan_wittich(state))

    log("\n[2/3] Breitengüßbach PDF …")
    all_findings.extend(scan_breitenguessbach(state))

    log("\n[3/3] Hirschaid PDF …")
    all_findings.extend(scan_hirschaid(state))

    state['last_scan'] = datetime.datetime.now().isoformat()
    save_state(state)
    save_findings_log(all_findings)

    log(f"\nGesamt neue Treffer: {len(all_findings)}")
    send_email(all_findings)
    log("Fertig.")


if __name__ == '__main__':
    main()
