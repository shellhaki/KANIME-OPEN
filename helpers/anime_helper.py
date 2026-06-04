import json
import asyncio
import time
import os
import re
import sqlite3
from bs4 import BeautifulSoup
import httpx
from playwright.async_api import async_playwright,TimeoutError
from utils.helper import deobfuscate,extract_info, decode_internal_id, generate_internal_id
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from db import get_db_direct
from utils.download_runtime import (
    ANIME_INFO_CACHE,
    COOKIE_CACHE,
    EPISODE_SESSION_CACHE,
    REDIRECT_LINK_CACHE,
)
import traceback
async def cookies_expired(db):
    """Check if __ddg2 cookie is expired"""
    now = time.time()
    
    cursor = await db.execute(
        "SELECT value, expires FROM cookies WHERE name = ?", 
        ("__ddg2",)
    )
    row = await cursor.fetchone()
    
    if not row:
        print(f"❌ __ddg2 cookie missing from database")
        return True
    
    exp = row["expires"]
    if not exp:
        print(f"❌ __ddg2 has no expiry field")
        return True
    
    is_expired = exp < now
    return is_expired


async def get_animepahe_cookies(db=None):
    """Get cookies with an in-memory first strategy, falling back to DB only when needed."""

    cached_cookies = await COOKIE_CACHE.get("animepahe_cookies")
    if cached_cookies:
        return cached_cookies

    # 1️⃣ If a DB connection exists, try the legacy cookie table first.
    if db is not None:
        try:
            cursor = await db.execute("SELECT COUNT(*) as count FROM cookies")
            row = await cursor.fetchone()

            if row and row["count"] > 0:
                if not await cookies_expired(db):
                    cursor = await db.execute("SELECT name, value FROM cookies")
                    rows = await cursor.fetchall()
                    cookies = {row["name"]: row["value"] for row in rows}
                    await COOKIE_CACHE.set("animepahe_cookies", cookies, 60 * 20)
                    return cookies
                else:
                    print("⚠️ Cookies expired, fetching new ones...")
        except Exception as e:
            print(f"⚠️ Cookie DB read skipped: {e}")

    # 2️⃣ Cookies expired or don't exist - use Playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            
            # Go to Animepahe
            await page.goto("https://animepahe.pw")
            
            # Wait for main content to load
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except TimeoutError:
                print("⚠️ Timeout waiting for DOMContentLoaded, continuing anyway...")
            
            # Small sleep to ensure cookies are set
            await asyncio.sleep(1)
            
            cookies = await context.cookies()
            await browser.close()

            cookie_map = {c['name']: c['value'] for c in cookies}
            await COOKIE_CACHE.set("animepahe_cookies", cookie_map, 60 * 20)

            if db is not None:
                try:
                    await db.execute("DELETE FROM cookies")

                    for cookie in cookies:
                        await db.execute(
                            "INSERT INTO cookies (name, value, expires) VALUES (?, ?, ?)",
                            (cookie['name'], cookie['value'], cookie.get('expires'))
                        )

                    await db.commit()
                except sqlite3.OperationalError as e:
                    print(f"⚠️ Skipping cookie DB write due to lock: {e}")
                except Exception as e:
                    print(f"⚠️ Skipping cookie DB write: {e}")
            
            print("✅ Used fresh cookies from animepahe server")

            return cookie_map
            
    except Exception as e:
        print(f"❌ Failed to get new cookies: {e}")

        if db is not None:
            try:
                cursor = await db.execute("SELECT name, value FROM cookies")
                rows = await cursor.fetchall()

                if rows:
                    print("⚠️ Using expired cached cookies as fallback")
                    cookies = {row["name"]: row["value"] for row in rows}
                    await COOKIE_CACHE.set("animepahe_cookies", cookies, 60 * 5)
                    return cookies
            except Exception:
                pass

        return None  # No cookies available at all


async def get_actual_episode_count(external_id,db):
    try:
        if not external_id:
            return None
        cookies = await get_animepahe_cookies(db)
        
        async with httpx.AsyncClient(cookies=cookies, timeout=30) as client:
            res = await client.get(
                f"https://animepahe.pw/a/{external_id}",
                follow_redirects=False
            )
            redirect_url = res.headers.get("Location")
            path = urlparse(redirect_url).path
            external_id = path.rstrip("/").split("/")[-1]
            res = await client.get(
                f"https://animepahe.pw/api?m=release&id={external_id}",
            )
        if res.status_code != 200:
            return None
        data = res.json()

        return data.get("total")
    except httpx.ConnectTimeout:
        print("Connection error")
        return None
    except Exception as e:
        print(e)
        return None

async def get_cached_anime_info(id, db):
    try:
        if not id:
            return {"status": 400, "message": "No ID provided"}

        cache_key = f"anime_info:{id}"
        cached_row = await ANIME_INFO_CACHE.get(cache_key)
        if cached_row:
            return cached_row

        row = None
        if db:
            try:
                cursor = await db.execute("SELECT * FROM anime_info WHERE internal_id = ?", (id,))
                row = await cursor.fetchone()
            except Exception as e:
                print(f"⚠️ anime_info cache read skipped: {e}")
        
        if not row:
            episodes = await get_actual_episode_count(id, db)
            if not episodes:
                return {"status": 500, "message": "Failed to fetch episode count"}
            mal_id = await get_mal_id(id, db)
            async with httpx.AsyncClient() as client:
                response = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}")
                response.raise_for_status()
                response = response.json()

            data = response.get("data", {})
            title = (
                data.get("title_english")
                or data.get("title")
                or data.get("title_japanese")
                or "Unknown Title"
            )
            poster = data.get("images", {}).get("jpg", {}).get("image_url")

            result = {
                "status": 200,
                "internal_id": id,
                "external_id": id,
                "title": title,
                "episodes": episodes,
                "poster": poster,
            }

            await ANIME_INFO_CACHE.set(cache_key, result, 60 * 30)

            if db is not None:
                try:
                    await db.execute('''
                        INSERT INTO anime_info(internal_id, external_id, title, episodes, poster)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(external_id) DO UPDATE SET
                            title = excluded.title,
                            episodes = excluded.episodes,
                            poster = excluded.poster;
                        ''',
                        (id, id, title, episodes, poster))
                    await db.commit()
                except sqlite3.OperationalError as e:
                    print(f"⚠️ Skipping anime_info DB write due to lock: {e}")
                except Exception as e:
                    print(f"⚠️ Skipping anime_info DB write: {e}")

            return result

        # Check if external_id exists
        external_id = row["external_id"]
        if not external_id:
            return {"status": 400, "message": "No external_id found for this anime"}
        
        # Get actual episode count
        episodes = await get_actual_episode_count(id, db)
        if not episodes:
            return {"status": 500, "message": "Failed to fetch episode count"}
        
        # Update if episode count changed
        if int(episodes) != int(row["episodes"]):
            row = dict(row)
            row["episodes"] = episodes
            if db is not None:
                try:
                    await db.execute(
                        "UPDATE anime_info SET episodes = ? WHERE internal_id = ?",
                        (episodes, id)
                    )
                    await db.commit()
                except sqlite3.OperationalError as e:
                    print(f"⚠️ Skipping episode update due to lock: {e}")
                except Exception as e:
                    print(f"⚠️ Skipping episode update: {e}")

        if not row:
            return {"status": 404, "message": "Id not registered. Search the anime first"}
        result = {"status": 200, **dict(row)}
        await ANIME_INFO_CACHE.set(cache_key, result, 60 * 30)
        return result
    
    except Exception as e:
        print(f"Error in get_cached_anime_info: {e}")
        traceback.print_exc()
        return {"status": 500, "message": f"Internal error: {str(e)}"}
async def get_episode_session(id, db):
    if not id:
        return None
    cache_key = f"episode_session:{id}"
    cached_session = await EPISODE_SESSION_CACHE.get(cache_key)
    if cached_session is not None:
        return cached_session

    cookies = await get_animepahe_cookies(db)
    async with httpx.AsyncClient(cookies=cookies) as client:
        res = await client.get(
                f"https://animepahe.pw/a/{id}",
                follow_redirects=False
            )
        redirect_url = res.headers.get("Location")
        path = urlparse(redirect_url).path
        id = path.rstrip("/").split("/")[-1]
        res = await client.get(f"https://animepahe.pw/api?m=release&id={id}")
        if res.status_code == 404:
            return []
        res.raise_for_status()
        try:
            data = res.json()
        except Exception:
            return []
        episodes = data.get("data")
        if not episodes:
            return []
        episode_id = episodes[0].get("session")
        if not episode_id:
            return []
        url = f"https://animepahe.pw/play/{id}/{episode_id}"
        res = await client.get(url, cookies=cookies)
        res.raise_for_status()

    episode_session = await asyncio.to_thread(_parse_episode_html, res.text)
    await EPISODE_SESSION_CACHE.set(cache_key, episode_session, 60 * 30)
    return episode_session

def _parse_episode_html(content):
    soup = BeautifulSoup(content, "html.parser")
    div = soup.find("div", id="scrollArea")
    if not div:
        return {"status": 404, "message": "No scroll Area found"}
    
    a_tags = div.find_all("a", class_="dropdown-item")
    episode_session = []
    
    for idx, a_tag in enumerate(a_tags):
        episode_text = a_tag.text.strip().split(" ")[1]  # "45" or "12.5" or "1-2"
        
        # Determine if special format
        is_special = "-" in episode_text or "." in episode_text
        
        episode_dict = {
            "session": a_tag["href"].split("/")[3],
            "episode": idx + 1,              # ← USE INDEX! (1, 2, 3, 4...)
            "episode_label": episode_text,   # ← KEEP ORIGINAL ("45", "12.5")
            "is_special": is_special
        }
        episode_session.append(episode_dict)
    
    return episode_session

async def get_pahewin_link(external_id, episode_id,db,quality):
    if not episode_id or not external_id:
        return None
    
    cookies = await get_animepahe_cookies(db)
    
    # Use httpx for async HTTP request
    async with httpx.AsyncClient() as client:
        external_id = await get_external_id(client, external_id, cookies )
        url = f"https://animepahe.pw/play/{external_id}/{episode_id}"
        res = await client.get(url, cookies=cookies, timeout=10)
        html = res.text
    
    # Offload BeautifulSoup parsing to thread pool
    link = await asyncio.to_thread(_parse_pahewin_html, html, url,quality)
    return link

def _parse_pahewin_html(html, url, quality="720p"):
    soup = BeautifulSoup(html, "html.parser")
    dropdown = soup.find("div", id="pickDownload")
    if not dropdown:
        return None
    
    links = dropdown.find_all("a", class_="dropdown-item")
    
    # Get all available qualities with their links
    available = []
    for a in links:
        text = a.get_text(" ", strip=True).lower()
        if "eng" not in text:  # Skip English dubs
            # Extract resolution (360, 720, 1080, 400, 800, etc.)
            match = re.search(r'(\d+)p', text)
            if match:
                resolution = int(match.group(1))
                available.append({
                    "resolution": resolution,
                    "link": a["href"],
                    "text": text
                })
    
    if not available:
        return None
    
    # Find closest match to requested quality
    target = int(quality.replace("p", ""))
    closest = min(available, key=lambda x: abs(x["resolution"] - target))
    
    return closest["link"]

async def get_kiwi_url(pahe_url):
    if not pahe_url:
        print("No pahe.win link")
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*"
    }

    # Async HTTP request with httpx
    async with httpx.AsyncClient() as client:
        res = await client.get(pahe_url, timeout=30, headers=headers,follow_redirects=True)
        html = res.text
    # Offload BeautifulSoup parsing to thread pool
    return await asyncio.to_thread(_parse_kiwi_url, html)


def _parse_kiwi_url(html):
    """Synchronous HTML parsing - runs in thread pool"""
    soup = BeautifulSoup(html, "html.parser")
    info = soup.find("script")
    if not info or "kwik" not in info.text:
        return None
    m = re.search(r"https?://(?:www\.)?kwik\.cx[^\s\"');]+", info.text)
    return m.group(0) if m else None

async def get_kiwi_info(kiwi_url):
    try:
        if not kiwi_url:
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        }

        html = None
        cookies = None
        last_exc = None

        for attempt in range(3):  # retry up to 3 times
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        f"https://kwik-proxy.pages.dev/?url={kiwi_url}",
                        timeout=20 + (attempt * 10),  # 20s, 30s, 40s
                        headers=headers
                    )
                    html = res.text
                    cookies = res.cookies
                break  # success, stop retrying
            except httpx.ReadTimeout as e:
                last_exc = e
                print(f"Kiwi timeout on attempt {attempt + 1}, retrying...")
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s backoff
                continue

        if html is None:
            print(f"Kiwi failed after 3 attempts: {last_exc}")
            return None

        result = await asyncio.to_thread(_parse_and_deobfuscate_kiwi, html, cookies)
        return result

    except IndexError:
        print(html)
        print("Script is out of range -2")
        return None
    except Exception as e:
        print("Kiwi error Occurred", e)
        traceback.print_exc()
        return None
def _parse_and_deobfuscate_kiwi(html, cookies):
    """Synchronous parsing and deobfuscation - runs in thread pool"""
    html_soup = BeautifulSoup(html, "html.parser")
    scripts = html_soup.find_all("script")
    obf_js = scripts[-1].text
    deobf_js = deobfuscate(obf_js)
    return {
        **extract_info(deobf_js),
        "kwik_session": cookies.get("kwik_session")
    }

async def get_redirect_link(url, id, episode, db, snapshot, quality, anime_title=None, max_retries=3):
    if not url or not id or not episode:
        print("No url,episode or id detected ending now")
        return None

    cache_key = f"redirect:{id}:{episode}:{quality}:{url}"
    cached_redirect = await REDIRECT_LINK_CACHE.get(cache_key)
    if cached_redirect:
        return cached_redirect
    
    info = await get_kiwi_info(url)
    if not info:
        return {
            "status": 500,
            "message": "Server timed out, retry request"
        }
    
    base_url = "https://kwik-test.vercel.app/kwik"
    payload = {
        "kwik_url": url,
        "token": info.get("token"),
        "kwik_session": info.get("kwik_session")
    }
    
    # ✨ NEW: Retry logic
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    base_url,
                    content=json.dumps(payload),
                    timeout=30,  # Increased timeout
                    headers={"Content-Type": "application/json"}
                )
            
            if res.status_code != 200:
                print(res.text)
                return {
                    "status": 500,
                    "message": "Server timed out"
                }
            
            # Success! Break out of retry loop
            break
            
        except httpx.ReadTimeout:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # 2s, 4s, 6s
                print(f"⏰ Timeout on attempt {attempt + 1}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                print(f"❌ Failed after {max_retries} attempts")
                return {
                    "status": 500,
                    "message": "Request timed out after multiple retries"
                }
        except Exception as e:
            print(f"❌ Error: {e}")
            return {
                "status": 500,
                "message": str(e)
            }
    
    data = res.json()
    size = info.get("size")
    direct_link = data.get("download_link")

    direct_link = await modify_filename_in_url(direct_link, anime_title, episode, quality)
    direct_link = f"https://k-proxy-v2.pages.dev?vid={direct_link}"

    result = {
        "direct_link": direct_link,
        "episode": episode,
        "snapshot": snapshot,
        "quality": quality,
        "status": 200,
        "size": size
    }
    await REDIRECT_LINK_CACHE.set(cache_key, result, 60 * 30)
    return result

async def modify_filename_in_url(url, anime_title, episode, quality):
    """
    Modifies the file= query parameter in the URL to use K-[ANIME.ME] branding
    """
    if not anime_title:
        return url

    raw_title = anime_title
    anime_title = re.sub(r'[\\/:*?"<>|]', '_', raw_title) \
           .replace(" ", "_") \
           .lower()
    
    # ✨ UPDATED: Use episode instead of episode number
    custom_filename = f"[K-ANIME.ME]_{anime_title}_Episode_{episode}.mp4"
    
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    query_params['file'] = [custom_filename]
    new_query = urlencode(query_params, doseq=True)
    modified_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    
    return modified_url

async def get_mal_id(external_id: str, db) -> str | None:
    try:        
        cookies = await get_animepahe_cookies(db)
        if not external_id:
            return None
        animepahe_url = f"https://animepahe.pw/a/{external_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=20, follow_redirects= True) as client:

            response = await client.get(animepahe_url)
            response.raise_for_status()  # 👈 turns 400–500 into exceptions

        html = response.text

        # --- BeautifulSoup in a thread ---
        def extract_mal_id():
            soup = BeautifulSoup(html, "html.parser")
            external_links = soup.find("p", class_="external-links")
            if not external_links:
                return None

            for a in external_links.find_all("a", href=True):
                href = a["href"]
                if "myanimelist.net/anime/" in href:
                    return href.rstrip("/").split("/")[-1]

            return None

        mal_id = await asyncio.to_thread(extract_mal_id)
        return mal_id

    except httpx.HTTPStatusError as e:
        # 400–500 errors land here
        print(f"HTTP error: {e.response.status_code}")
        return None

    except httpx.RequestError as e:
        # network issues, DNS, timeout, etc.
        print(f"Request failed: {e}")
        traceback.print_exc()
        return None

    except Exception as e:
        # anything else you didn't foresee
        print(f"Unexpected error: {e}")
        return None

async def get_actual_episode_from_session(anime_id: int, session: str, db) -> int:
    # Get all episodes for this anime
    episodes = await get_episode_session(anime_id, db)
    
    # Check if episodes exist and is a list
    if not episodes or not isinstance(episodes, list):
        return 0
    
    # Loop through episodes to find matching session
    for idx, ep in enumerate(episodes):
        if ep["session"] == session:
            return idx + 1  # Return position (1-indexed)
    
    return 0  # Session not found

async def validate_and_get_anime_info(internal_id: str, db):
    """
    Validate anime ID and return full anime info
    Returns: Dict with anime info if valid, None if invalid
    """
    try:
        # Get MAL ID
        try:
            mal_id = await get_mal_id(internal_id, db)
        except Exception as e:
            print(f"Failed to get MAL ID: {e}")
            return None
        
        # Fetch anime data
        async with httpx.AsyncClient() as client:
            cookies = await get_animepahe_cookies(db)
            external_id = await get_external_id(client, internal_id, cookies)
            try:
                jikan_response, pahe_response = await asyncio.gather(
                    client.get(f"https://api.jikan.moe/v4/anime/{mal_id}", timeout=10.0),
                    client.get(
                        f"https://animepahe.pw/api?m=release&id={external_id}&sort=episode_desc&page=1",
                        timeout=10.0,
                        cookies=cookies
                    )
                )
                
                jikan_response.raise_for_status()
                pahe_response.raise_for_status()
                
                anime_data = jikan_response.json()['data']
                episode_data = pahe_response.json()
            except httpx.HTTPError as e:
                print(f"Failed to fetch anime data: {e}")
                return None
        
        # Process episodes
        episodes = []
        all_episodes = await get_episode_session(internal_id, db)
        session_to_index = {ep["session"]: idx + 1 for idx, ep in enumerate(all_episodes)}
        
        for ep in episode_data.get('data', []):
            actual_episode = session_to_index.get(ep['session'], 0)
            episodes.append({
                "episode_number": actual_episode,
                "duration": ep['duration'],
                "is_filler": bool(ep['filler']),
                "snapshot": ep['snapshot']
            })
        
        # Get broadcast day
        broadcast_day = None
        if anime_data['status'] == "Currently Airing" and anime_data.get('broadcast'):
            broadcast_day = anime_data['broadcast'].get('day')
        
        # Return rich info
        return {
            "is_valid": True,
            "internal_id": internal_id,
            "title": anime_data.get("title_english") or anime_data.get("title"),
            "synopsis": anime_data.get('synopsis', ''),
            "image_url": anime_data['images']['jpg']['large_image_url'],
            "genres": [genre['name'] for genre in anime_data.get('genres', [])],
            "duration": anime_data.get('duration', ''),
            "rating": anime_data.get('rating', ''),
            "score": anime_data.get('score'),
            "year": anime_data.get('year'),
            "broadcast_day": broadcast_day,
            "status": anime_data.get('status', ''),
            "episodes": episodes,
            "pagination": {
                "current_page": episode_data.get('current_page', 1),
                "last_page": episode_data.get('last_page', 1),
                "per_page": episode_data.get('per_page', 30),
                "total": episode_data.get('total', 0)
            }
        }
    
    except Exception as e:
        print(f"Error validating anime ID: {e}")
        return None

async def validate_and_get_anime_total(internal_id: str, db):
    """
    Validate anime ID and return technical info + pagination
    Removes Jikan/MAL dependencies for faster internal processing.
    """
    try:
        # 1. Decode the internal ID to get the external session_id
        
        # 2. Fetch only technical data from AnimePahe
        async with httpx.AsyncClient() as client:
            cookies = await get_animepahe_cookies(db)
            external_id = await get_external_id(client, internal_id, cookies)
            try:
                # We only need the first page to get the 'total' and validate the session
                response = await client.get(
                    f"https://animepahe.pw/api?m=release&id={external_id}&sort=episode_desc&page=1",
                    timeout=10.0,
                    cookies=cookies
                )
                response.raise_for_status()
                episode_data = response.json()
            except httpx.HTTPError as e:
                print(f"Failed to fetch AnimePahe data: {e}")
                return None
        
        # 3. Process episodes (Optional: keep only if you need the specific list)
        episodes = []
        all_episodes = await get_episode_session(internal_id, db)
        # Using dict(ep) if your DB returns sqlite3.Row objects
        session_to_index = {ep["session"]: idx + 1 for idx, ep in enumerate(all_episodes)}
        
        for ep in episode_data.get('data', []):
            actual_episode = session_to_index.get(ep['session'], 0)
            episodes.append({
                "episode_number": actual_episode,
                "duration": ep['duration'],
                "is_filler": bool(ep['filler']),
                "snapshot": ep['snapshot']
            })
        
        # 4. Return the technical summary
        return {
            "is_valid": True,
            "internal_id": internal_id,
            "session_id": internal_id,
            "episodes": episodes,
            "pagination": {
                "current_page": episode_data.get('current_page', 1),
                "last_page": episode_data.get('last_page', 1),
                "per_page": episode_data.get('per_page', 30),
                "total": episode_data.get('total', 0) # This is your 'total' count
            }
        }
    
    except Exception as e:
        print(f"Error validating anime ID: {e}")
        return None

async def search_anime_pahe(query: str, db):
    """
    Internal helper to search AnimePahe and sync with local DB.
    Used for UI searches and background repair logic.
    """
    if not query:
        return []

    try:
        cookies = await get_animepahe_cookies(db)
        async with httpx.AsyncClient(cookies=cookies, timeout=30) as client:
            # Note: Ensure encodeURIComponent is imported or use urllib.parse.quote
            from urllib.parse import quote
            encoded_query = quote(query)
            
            res = await client.get(f"https://animepahe.pw/api?m=search&q={encoded_query}")
            res.raise_for_status() # Raise error for bad status codes
            results = res.json()

        info = results.get('data')
        if not info:
            return []

        search_results = []
        for i in info:
            external_id = i.get("id")
            
            # 1. Check if we already know this anime
            cursor = await db.execute(
                "SELECT internal_id FROM anime_info WHERE external_id = ?", (external_id,)
            )
            row = await cursor.fetchone()
            
            # 2. Logic for episode count (Checking if airing/0)
            episodes = await get_actual_episode_count(external_id, db) if i.get(
                "episodes") == 0 or i.get("status") == "Currently Airing" else i.get("episodes")

            # 3. Sync with local anime_info table
            if not row:
                await db.execute('''
                    INSERT INTO anime_info(internal_id, external_id, title, episodes, poster)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        title = excluded.title,
                        episodes = excluded.episodes,
                        poster = excluded.poster
                ''', (external_id, external_id, i.get("title"), episodes, i.get("poster")))
                await db.commit()
            else:
                external_id = row["internal_id"]

            # 4. Build the object
            search_results.append({
                "internal_id": external_id,
                "external_id": external_id, # Very important for your repair logic!
                "title": i.get("title"),
                "episodes": episodes,
                "status": i.get("status"),
                "year": i.get("year"),
                "poster": i.get("poster"),
                "rating": i.get("score")
            })
            
        return search_results

    except Exception as e:
        print(f"Internal Search Error: {e}")
        traceback.print_exc()
        return []

async def repair_anime_id(anime_name, target_mal_id, db):
    """
    Searches for an anime and matches the MAL ID to find the new provider ID.
    """
    # 1. Search AnimePahe for the name
    # Assume this returns a list of results with 'id' and 'title'
    search_results = await search_anime_pahe(anime_name, db) 
    
    for result in search_results:
        scraped_mal_id = await get_mal_id(result["external_id"], db)
        # 3. The DNA Match
        if str(scraped_mal_id) == str(target_mal_id):
            return result['external_id'] # Found it!
            
    return None # Mission failed



async def check_and_notify_episodes():
    """Check all tracked animes for new episodes and notify subscribers"""
    
    from dual_db import fetch, execute
    from utils.telegram import send_anime_alert
    import asyncio
    # Get all tracked animes
    tracked = await fetch("SELECT * FROM tracked_animes")
    for anime in tracked:
        anime_id = anime['anime_id']
        anime_id = str(anime_id)
        anime_name = anime['anime_name']
        old_episode = anime['latest_episode']
        db = await get_db_direct()
        # Fetch latest info from API
        anime_info = await validate_and_get_anime_total(anime_id, db)
        if anime_info is None:
            print(f"⚠️ ID {anime_id} failed for {anime_name}. Starting Repair...")
            
            # Trigger Search & Rescue
            new_id = await repair_anime_id(anime_name, anime['mal_id'], db)
            new_id = str(new_id)
            if new_id:
                print(f"✅ Repaired! New ID: {new_id}")
                # Update the DB so we don't have to repair again next time
                await execute(
                    "UPDATE tracked_animes SET anime_id = $1 WHERE mal_id = $2",
                    [new_id, anime['mal_id']]
                )
                # Re-run the validation with the NEW ID
                anime_info = await validate_and_get_anime_total(new_id, db)
            else:
                print(f"❌ Could not repair {anime_name}. Skipping.")
                continue
        
        new_episode = anime_info['pagination']['total']

        # NEW EPISODE?
        if new_episode > old_episode:
            print(f"🎬 {anime_name}: {old_episode} → {new_episode}")
        
            # Update tracked anime
            await execute(
                "UPDATE tracked_animes SET latest_episode = $1, last_checked = CURRENT_TIMESTAMP WHERE anime_id = $2",
                [new_episode, anime_id]
            )
            
            # Get ALL users subscribed to this anime
            subscribers = await fetch(
                "SELECT s.chat_id FROM subscribers s JOIN user_anime_subscriptions uas ON s.chat_id = uas.chat_id WHERE uas.anime_id = $1",
                [anime_id]
            )
            
            # Send to each subscriber (with delay to avoid rate limit)
            for sub in subscribers:
                chat_id = sub['chat_id']
                
                await send_anime_alert(
                    chat_id,
                    f"{anime_name} Episode {new_episode}",
                    f"https://k-anime.me/anime/{anime_id}"
                )
                
                # Log notification
                await execute(
                    "INSERT INTO notification_log (chat_id, anime_id, message_text, status) VALUES ($1, $2, $3, $4)",
                    [chat_id, anime_id, f"{anime_name} Episode {new_episode}", "sent"]
                )
                
                # Delay to avoid Telegram rate limit (30 messages/second max)
                # 0.05 seconds = 20 messages/second (safe)
                await asyncio.sleep(0.05)

# --- THE HF VAULT PATCH ---
VAULT_PATCHES = {
    "4072": {
        "hf_folder": "Jujutsu_Kaisen_season_3",
        "patched_episodes": [1, 2, 3, 4, 5, 6, 7]
    }
}


async def resolve_hf_to_aws(hf_url: str):
    """Hits the HF link, grabs the hidden AWS link, and returns it instantly."""
    async with httpx.AsyncClient() as client:
        # We use a HEAD request so we don't download the video, we just read the directions!
        response = await client.head(hf_url, follow_redirects=True)
        return str(response.url)

async def get_vault_links(anime_id: str, episode_num: int, quality: str):
    """Traffic Cop to check if episode exists in HF Vault"""
    if anime_id in VAULT_PATCHES and int(episode_num) in VAULT_PATCHES[anime_id]["patched_episodes"]:
        base_url = "https://huggingface.co/datasets/A-Y-A-N-O-K-O-J-I/jjk-vault/resolve/main"
        folder = VAULT_PATCHES[anime_id]["hf_folder"]
        ep_str = f"ep_{int(episode_num):02d}.mp4"
        
        # 1. Build the base HF link
        base_link = f"{base_url}/{folder}/{quality}/{ep_str}?download=true"
        
        # 2. Scout the fresh AWS link on the fly!
        # (For the individual download route, you only need to resolve the specific quality requested)
        raw_aws_link = await resolve_hf_to_aws(base_link)
        
        return {
            "status": 200,
            "direct_link": raw_aws_link, # Frontend now sees the raw AWS link!
            "quality": quality,
            "size": "N/A",
            "episode": episode_num,
            "provider": "hf_vault"
        }
    return None

def parse_resolutions_sync(html: str):
    """
    Sync helper to handle the CPU-heavy BeautifulSoup parsing.
    """
    if not html:
        return []
        
    soup = BeautifulSoup(html, "html.parser")
    stream_menu = soup.find("div", id="resolutionMenu")
    
    if not stream_menu:
        return []
        
    stream_elements = stream_menu.find_all("button")
    stream_info = []
    
    for stream_element in stream_elements:
        # We extract the data attributes you identified
        stream_info_dict = {
            "audio": stream_element.get("data-audio", "jpn"),
            "resolution": stream_element.get("data-resolution", "720") + "p",
            "kwik_url": stream_element.get("data-src", "")
        }
        stream_info.append(stream_info_dict)
        
    return stream_info

async def get_stream_links(internal_id: str, episode_id: str, db):
    """
    Fetches all available resolutions and Kwik links for a specific episode.
    """
    
    # 1. Get the session cookies from your DB
    cookies = await get_animepahe_cookies(db)
    
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://animepahe.pw/"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            external_id = await get_external_id(client, internal_id, cookies)
            url = f"https://animepahe.pw/play/{external_id}/{episode_id}"
            response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                print(f"Error: AnimePahe returned {response.status_code}")
                return []
                
            html = response.text

        # 2. Run the BS4 parsing in a separate thread to keep FastAPI fast
        stream_links = await asyncio.to_thread(parse_resolutions_sync, html)
        return stream_links

    except Exception as e:
        print(f"Extraction failed: {e}")
        return []

# 1. Keep your math and regex exactly as they are (Synchronous)
def decode_base_62(c: int, a: int) -> str:
    first_part = '' if c < a else decode_base_62(c // a, a)
    rem = c % a
    if rem > 35:
        second_part = chr(rem + 29)
    else:
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        second_part = chars[rem]
    return first_part + second_part

def extract_kwik_m3u8_sync(html_content: str):
    pattern = r"}\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)"
    matches = re.finditer(pattern, html_content, re.DOTALL)
    
    for match in matches:
        payload = match.group(1)
        radix = int(match.group(2))
        count = int(match.group(3))
        dictionary = match.group(4).split('|')
        
        payload = payload.replace("\\'", "'").replace("\\\\", "\\")
        
        word_map = {}
        for i in range(count):
            token = decode_base_62(i, radix)
            real_word = dictionary[i] if i < len(dictionary) and dictionary[i] else token
            word_map[token] = real_word
            
        def replacer(m):
            word = m.group(0)
            return word_map.get(word, word)
            
        unpacked_script = re.sub(r'\b\w+\b', replacer, payload)
        url_match = re.search(r'(https?://[^\s\'"]+\.m3u8)', unpacked_script)
        
        if url_match:
            return url_match.group(1)
            
    return None

# 2. THE ASYNC WRAPPER
async def extract_kwik_m3u8(html_content: str):
    """
    This is the async function you will actually call in your routes.
    It pushes the heavy regex/math to a background worker thread.
    """
    # asyncio.to_thread runs the sync function in the background
    m3u8_link = await asyncio.to_thread(extract_kwik_m3u8_sync, html_content)
    print(m3u8_link)
    return m3u8_link

import httpx
import urllib.parse
import re

DOWNLOAD_PROXY_BASE = "https://kwik-test.spcfy.eu/proxy?vid="


def build_kanime_filename(anime_title: str | None, episode, extension: str = "mp4") -> str:
    raw_title = anime_title or "anime"
    anime_title = re.sub(r'[\\/:*?"<>|]', '_', raw_title) \
           .replace(" ", "_") \
           .lower()
    return f"[K-ANIME.ME]_{anime_title}_Episode_{episode}.{extension}"


def stream_m3u8_to_download_url(m3u8_url: str, anime_title: str | None, episode) -> str | None:
    if not m3u8_url:
        return None

    parsed = urlparse(m3u8_url)
    path = parsed.path
    if "/stream/" not in path:
        return None

    path = path.replace("/stream/", "/mp4/", 1)
    if path.endswith("/uwu.m3u8"):
        path = path[:-len("/uwu.m3u8")]
    elif path.endswith(".m3u8"):
        path = path.rsplit("/", 1)[0]

    query = urlencode({"file": build_kanime_filename(anime_title, episode)})
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def proxied_download_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(DOWNLOAD_PROXY_BASE):
        return url
    return f"{DOWNLOAD_PROXY_BASE}{urllib.parse.quote(url, safe='')}"


def _pick_stream_link(stream_links: list[dict], quality: str) -> dict | None:
    if not stream_links:
        return None

    for link in stream_links:
        if link.get("resolution") == quality:
            return link

    try:
        target = int(quality.replace("p", ""))
        return min(
            stream_links,
            key=lambda link: abs(int(str(link.get("resolution", "0p")).replace("p", "")) - target)
        )
    except Exception:
        return stream_links[0]


async def get_download_link_from_stream(internal_id: str, episode_session: str, episode, quality: str, anime_title: str | None, snapshot: str | None, db=None):
    stream_links = await get_stream_links(internal_id, episode_session, db)
    selected_stream = _pick_stream_link(stream_links, quality)
    if not selected_stream:
        return None

    kwik_url = selected_stream.get("kwik_url")
    if not kwik_url:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://animepahe.pw/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        response = await client.get(kwik_url)
        response.raise_for_status()

    m3u8_link = await extract_kwik_m3u8(response.text)
    direct_link = proxied_download_url(
        stream_m3u8_to_download_url(m3u8_link, anime_title, episode)
    )
    if not direct_link:
        return None

    return {
        "direct_link": direct_link,
        "episode": episode,
        "snapshot": snapshot or "",
        "quality": selected_stream.get("resolution") or quality,
        "status": 200,
        "size": "N/A",
        "provider": "stream_mp4",
    }

async def get_proxied_m3u8(master_url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://kwik.cx/" 
    }
    
    proxy_base = "https://a-y-a-n-o-k-o-j-i-sophia-md.hf.space/proxy?vid="
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(master_url, headers=headers)
        
        if response.status_code != 200:
            return None
            
        lines = response.text.splitlines()
        proxied_lines = []
        
        for line in lines:
            # 1. Handle Encryption Key Tags — match any URI, no domain check
            if line.startswith("#EXT-X-KEY"):
                match = re.search(r'URI="(.*?)"', line)
                if match:
                    original_uri = match.group(1)
                    encoded_uri = urllib.parse.quote(original_uri, safe='')
                    proxy_base_for_monkey = "https://kwik-proxy.pxxl.click/proxy?vid="
                    proxied_uri = f"{proxy_base_for_monkey}{encoded_uri}"
                    fixed_line = line.replace(f'URI="{original_uri}"', f'URI="{proxied_uri}"')
                    proxied_lines.append(fixed_line)
                else:
                    proxied_lines.append(line)
                    
            # 2. Normal metadata tags — pass through
            elif line.startswith("#"):
                proxied_lines.append(line)
                
            # 3. Any non-empty line that looks like a URL (segments, sub-playlists)
            elif line.strip() and line.strip().startswith("http"):
                encoded_url = urllib.parse.quote(line.strip(), safe='')
                proxied_lines.append(f"{proxy_base}{encoded_url}")

            # 4. Relative URLs (just in case)
            elif line.strip() and not line.startswith("#"):
                base_url = master_url.rsplit("/", 1)[0]
                absolute_url = f"{base_url}/{line.strip()}"
                encoded_url = urllib.parse.quote(absolute_url, safe='')
                proxied_lines.append(f"{proxy_base}{encoded_url}")
                
            else:
                proxied_lines.append(line)
                
        return "\n".join(proxied_lines)
# --- Usage Example ---
# new_playlist = await get_proxied_m3u8("https://vault-99.owocdn.top/stream/.../master.m3u8")
# print(new_playlist)

from urllib.parse import urlparse

async def get_external_id(client, id, cookies):
    res = await client.get(
        f"https://animepahe.pw/a/{id}",
        follow_redirects=False,
        cookies=cookies
    )
    redirect_url = res.headers.get("Location")
    if not redirect_url:
        return None  # or raise an error if you prefer

    path = urlparse(redirect_url).path
    external_id = path.rstrip("/").split("/")[-1]

    return external_id
