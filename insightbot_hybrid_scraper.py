# Usage:
#   python insightbot_hybrid_scraper.py --mode train
#   python insightbot_hybrid_scraper.py --mode test
import mysql
import requests, time, random, re, math, json, argparse
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dateutil import parser as dt_parser
import dateparser
import pandas as pd

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/115.0 Safari/537.36"
}

# ---------- Training Sites (40) ----------
TRAINING_SITES = [
    "https://www.cnn.com", "https://www.bbc.com", "https://www.nytimes.com",
    "https://www.theguardian.com", "https://www.reuters.com", "https://www.washingtonpost.com",
    "https://www.forbes.com", "https://techcrunch.com", "https://thenextweb.com", "https://medium.com",
    "https://dev.to", "https://mashable.com", "https://www.wsj.com", "https://www.wired.com",
    "https://www.npr.org", "https://www.vox.com", "https://www.bloomberg.com",
    "https://seekingalpha.com", "https://www.engadget.com", "https://www.theverge.com",
    "https://www.ft.com", "https://arstechnica.com", "https://www.cnet.com", "https://slashdot.org",
    "https://www.huffpost.com", "https://www.aljazeera.net", "https://www.skynewsarabia.com",
    "https://www.alarabiya.net",
    "https://www.akhbaar24.com", "https://www.middle-east-online.com", "https://www.thenationalnews.com",
    "https://arabic.cnn.com", "https://www.bbc.com/arabic", "https://www.masaar.com", "https://9elp.com",
    "https://www.rt.com", "https://tass.com", "https://www.rbc.ru", "https://meduza.io", "https://echo.msk.ru"]

# ---------- Testing Sites (10 unseen, from your file) ----------
TESTING_SITES = [
    "https://www.independent.co.uk", "https://www.chinadaily.com.cn",
    "https://www.japantimes.co.jp", "https://www.france24.com",
    "https://www.dw.com", "https://www.haaretz.com",
    "https://www.scmp.com", "https://www.lemonde.fr",
    "https://www.latimes.com", "https://www.timesofindia.com"
]

# ---------- Heuristic Config ----------
BAD_EXT_RE = re.compile(r".*\.(jpg|jpeg|png|gif|svg|pdf|mp4|mp3|zip|rss|ico)$", re.I)
BAD_SUBSTRINGS = ["mailto:", "tel:", "#", "signup", "login", "terms", "privacy", "javascript:"]

SECTION_HINTS = ["/section/", "/topic/", "/tag/", "/tags/", "/category/", "/categories/", "/topics/", "/collections/",
                 "/series/"]
POSITIVE_SIGNS = ["/news/", "/article/", "/story/", "/world/", "/politics/", "/business/", "/202"]

BAD_PHRASES = [
    "related articles", "you may also like", "more stories",
    "follow us", "share this", "advertisement", "sponsored content",
    "recommended", "read more", "watch:", "photo:", "video:"
]

from dateutil import parser, tz

TZINFOS = {
    "EST": tz.gettz("America/New_York"),
    "EDT": tz.gettz("America/New_York"),
    "CST": tz.gettz("America/Chicago"),
    "CDT": tz.gettz("America/Chicago"),
    "MST": tz.gettz("America/Denver"),
    "MDT": tz.gettz("America/Denver"),
    "PST": tz.gettz("America/Los_Angeles"),
    "PDT": tz.gettz("America/Los_Angeles"),
    "PT": tz.gettz("America/Los_Angeles"),
    "ET": tz.gettz("America/New_York")
}


def clean_date(raw_date):
    if not raw_date or str(raw_date).strip() == "":
        return None
    try:
        # Parse any language automatically
        dt = dateparser.parse(raw_date)
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            return None
    except Exception as e:
        print(f"Date parse error: {raw_date} → {e}")
        return None



# ---------- Helpers ----------
def fetch_url(session, url, retries=2, timeout=12):
    for _ in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            time.sleep(1 + random.random())
        except Exception:
            time.sleep(1 + random.random())
    return None


def is_probable_article(href, anchor_text=""):
    """Decide if a link looks like a real article structurally."""
    if not href:
        return False
    href_l = href.lower()
    if BAD_EXT_RE.match(href_l) or any(b in href_l for b in BAD_SUBSTRINGS):
        return False
    parsed = urlparse(href_l)
    path = parsed.path or ""
    # Reject obvious section/category pages
    if any(seg in path for seg in SECTION_HINTS):
        return False
    if path.endswith("/") and path.count("/") <= 3:
        return False  # e.g. /politics/, /world/, /business/
    # Positive signals
    if re.search(r"/\d{4}/\d{2}/\d{2}/", path):  # dated article
        return True
    if any(k in href_l for k in POSITIVE_SIGNS):
        return True
    if path.count("-") >= 2 and len(path) > 25:  # long slug with hyphens
        return True
    if any(k in anchor_text.lower() for k in ["full story", "read more", "headline"]):
        return True
    return False


def clean_title(text):
    if not text:
        return "N/A"
    t = " ".join(text.split())
    return re.sub(r"\s+[–|-|—|•|·]\s+.*$", "", t).strip()


def extract_title(soup):
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return clean_title(h1.get_text(" ", strip=True))
    meta = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if meta and meta.get("content"):
        return clean_title(meta.get("content"))
    if soup.title and soup.title.string:
        return clean_title(soup.title.string)
    return "N/A"


def score_container(el):
    ps = el.find_all("p")
    if not ps: return 0
    total_len = sum(len(p.get_text(" ", strip=True)) for p in ps)
    return total_len * (1 + math.log1p(len(ps))) - (len(el.find_all("a")) * 20)


def find_best_container(soup, min_score=200):
    candidates = []
    for tag in ("article", "main", "section", "div"):
        for el in soup.find_all(tag):
            try:
                s = score_container(el)
                if s > 0:
                    candidates.append((s, el))
            except:
                continue
    if not candidates: return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_el = candidates[0]
    return best_el if best_score >= min_score else None


def extract_paragraphs_from_el(el, min_len=40, max_paras=60):
    paras = []
    for p in el.find_all("p"):
        text = p.get_text(" ", strip=True)
        low = text.lower()
        if len(text) < min_len: continue
        if any(bad in low for bad in BAD_PHRASES): continue
        paras.append(text)
        if len(paras) >= max_paras: break
    return paras


# ---------- Article Extraction ----------

def extract_article(session, url):
    r = fetch_url(session, url)
    if not r: return None
    soup = BeautifulSoup(r.text, "lxml")

    title = extract_title(soup)
    container = find_best_container(soup)
    body_paras = extract_paragraphs_from_el(container) if container else []

    body_text = " ".join(body_paras).strip()

    # Content validation
    if title == "N/A" or len(title.split()) < 5:
        return None
    if len(body_paras) < 3 or len(body_text) < 200:
        return None

    published = ""
    meta_date = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_date and meta_date.get("content"):
        published = clean_date(meta_date["content"].strip())
    elif soup.find("time"):
        time_tag = soup.find("time")
        published_raw = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
        published = clean_date(published_raw)

    return {
        "url": url,
        "title": title,
        "body": body_text,
        "published": published,
        "length": len(body_text)
    }


# ---------- Homepage Link Collection ----------
def collect_article_links(session, site_url, limit=12):
    r = fetch_url(session, site_url)
    if not r: return []
    soup = BeautifulSoup(r.text, "lxml")
    links, seen = [], set()

    # Prefer h1/h2 anchors
    for tag in soup.find_all(["h1", "h2"]):
        a = tag.find("a", href=True)
        if not a: continue
        full = urljoin(site_url, a["href"])
        if full in seen: continue
        if is_probable_article(full, a.get_text(" ", strip=True)):
            seen.add(full);
            links.append(full)
            if len(links) >= limit: break

    # Fallback:
    if len(links) < limit:
        for a in soup.find_all("a", href=True):
            full = urljoin(site_url, a["href"])
            if full in seen: continue
            if is_probable_article(full, a.get_text(" ", strip=True)):
                seen.add(full);
                links.append(full)
                if len(links) >= limit: break
    return links


# ---------- Main Loop ----------
def scrape_all(sites, per_site_limit=3, pause=(1.0, 2.0)):
    session = requests.Session()
    results = []
    for site in sites:
        print(f"\n=== SITE: {site}")
        try:
            candidate_links = collect_article_links(session, site, limit=per_site_limit * 6)
            if not candidate_links:
                print("  ! no candidate links found")
            extracted = 0
            for link in candidate_links:
                if extracted >= per_site_limit: break
                print("   -> trying:", link)
                art = extract_article(session, link)
                if art:
                    art["source"] = urlparse(site).netloc
                    results.append(art)
                    extracted += 1
                    print(f"      ✓ extracted (len {art['length']})")
                else:
                    print("      ✗ skipped (failed validation)")
                time.sleep(random.uniform(*pause))
            if extracted == 0:
                print(f"  ⚠ warning: extracted 0 articles for {site}")
        except Exception as e:
            print("  error scraping site:", e)
    return results


# ---------- CLI Entrypoint ----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["train", "test"], default="train",
                        help="Choose scraping mode: train (40 sites) or test (10 unseen sites)")
    args = parser.parse_args()

    if args.mode == "train":
        print("Starting InsightBot Hybrid Scraper (TRAINING MODE, 40 sites)...")
        sites, out_csv, out_json = TRAINING_SITES, "news_hybrid_training.csv", "news_hybrid_training.json"
    else:
        print("Starting InsightBot Hybrid Scraper (TESTING MODE, 10 unseen sites, heuristics only)...")
        sites, out_csv, out_json = TESTING_SITES, "news_hybrid_testing.csv", "news_hybrid_testing.json"

    articles = scrape_all(sites, per_site_limit=3)
    if articles:
        df = pd.DataFrame(articles)
        df.to_csv(out_csv, index=False)
        with open(out_json, "w", encoding="utf-8") as f:
            for rec in articles:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"\nSaved {len(articles)} articles to {out_csv} and {out_json}")
    else:
        print("\nNo articles were extracted — check site blocking or heuristics.")
