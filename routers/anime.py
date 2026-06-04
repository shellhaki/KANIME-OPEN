import uuid
import json
from urllib.parse import quote
import traceback
import time
import io
import zipfile
import asyncio
import re
import shutil
from fastapi import APIRouter, Query, Depends,Request,WebSocket,WebSocketDisconnect,HTTPException
from fastapi import Body
from fastapi.responses import JSONResponse,StreamingResponse,Response,FileResponse
import httpx
from bs4 import BeautifulSoup
from db import get_db, get_db_direct
from helpers.anime_helper import get_episode_session
from helpers.anime_helper import get_animepahe_cookies,get_actual_episode_count,get_cached_anime_info, get_external_id
from helpers.anime_helper import get_mal_id,get_vault_links,get_stream_links,extract_kwik_m3u8, get_proxied_m3u8, get_download_link_from_stream, proxied_download_url
from utils.helper import generate_internal_id,encodeURIComponent,decode_internal_id
from utils.async_cache import AsyncTTLCache
from utils.download_runtime import (
    DOWNLOAD_SESSION_CACHE,
    DOWNLOAD_ZIP_CACHE,
)
from typing import Optional
from utils.limiter import limiter
router = APIRouter(prefix="/anime", tags=["Anime"])
route_cache = AsyncTTLCache()

AIRING_NOW_CACHE_TTL = 300
SEARCH_CACHE_TTL = 60
DOWNLOAD_CACHE_TTL = 120


class SkipRouteCache(Exception):
    def __init__(self, response):
        self.response = response

import sqlite3
import traceback
import httpx
from fastapi import Query, Depends
from fastapi.responses import JSONResponse

@router.get("/search", description="Searches for a specific anime", summary="Search anime")
async def anime_search(
    query: str = Query(..., description="Anime name for the search", examples="one piece"),
    db = Depends(get_db)
):
    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": 400, "message": "Query is a required parameter"}
        )

    async def fetch_search_result():
        search_result = []

        cookies = await get_animepahe_cookies(db)

        async with httpx.AsyncClient(cookies=cookies, timeout=30) as client:
            encode_query = await encodeURIComponent(query)
            res = await client.get(
                f"https://animepahe.pw/api?m=search&q={encode_query}"
            )

        try:
            results = res.json()
        except ValueError:
            print(res.status_code)
            print("❌ Not a JSON response:", res.text[:200])
            raise ValueError("Invalid JSON response from AnimePahe search")

        info = results.get("data")
        if not info:
            return []

        for i in info:
            internal_id = None

            # ── DB READ (safe, no need to skip reads) ─────────────────────
            cursor = await db.execute(
                "SELECT internal_id FROM anime_info WHERE external_id = ?",
                (i.get("id"),)
            )
            row = await cursor.fetchone()

            episodes = (
                await get_actual_episode_count(i.get("id"), db)
                if i.get("episodes") == 0 or i.get("status") == "Currently Airing"
                else i.get("episodes")
            )

            # ── DB WRITE (SAFE: skip if locked) ──────────────────────────
            if not row:
                internal_id = i.get("id")

                try:
                    await db.execute(
                        '''
                        INSERT INTO anime_info(internal_id, external_id, title, episodes, poster)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(external_id) DO UPDATE SET
                            title = excluded.title,
                            episodes = excluded.episodes,
                            poster = excluded.poster;
                        ''',
                        (
                            internal_id,
                            i.get("id"),
                            i.get("title"),
                            episodes,
                            i.get("poster")
                        )
                    )

                    try:
                        await db.commit()
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower():
                            print("⚠️ DB locked — skipping commit")
                        else:
                            raise

                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        print("⚠️ DB locked — skipping anime_info write")
                    else:
                        raise
            else:
                internal_id = row["internal_id"]

            filtered_search_result = {
                "id": internal_id,
                "title": i.get("title"),
                "episodes": episodes,
                "status": i.get("status"),
                "year": i.get("year"),
                "poster": i.get("poster"),
                "rating": i.get("score")
            }

            search_result.append(filtered_search_result)

        return search_result

    try:
        cache_key = f"anime_search:{query.strip().lower()}"

        search_result = await route_cache.get_or_set(
            cache_key,
            SEARCH_CACHE_TTL,
            fetch_search_result
        )

        if not search_result:
            return JSONResponse(status_code=404, content=[])

        return search_result

    except httpx.ConnectError:
        print("Connection error occurred")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": 500, "message": "Connection error occurred. Try again later"}
        )

    except httpx.ConnectTimeout:
        print("Connection timeout occurred")
        return JSONResponse(
            status_code=500,
            content={"status": 500, "message": "Connection timeout. Try again later"}
        )

    except Exception as e:
        print("Anime search error:", e)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": 500, "message": "Internal Server error"}
        )

        
@router.get("/download", description="Download anime using id gotten from search",summary="Download anime")
async def anime_download(id:str = Query(...,description="id for the anime from search",examples="4"),episode:int = Query(...,description="Anime episode number",examples=6),quality: str = Query("720p", regex="^(360p|720p|1080p)$"),db= Depends(get_db)):
    if not id or not episode or not quality:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message":"Id, episode or quality are required"
        })
    info = await get_cached_anime_info(id, None)
    if not info.get("status") == 200:
        return JSONResponse(
            status_code=info.get("status"),content={
                **info
            }
        )
    ep_count = info["episodes"]
    if int(episode) > int(ep_count):
        return JSONResponse(status_code=422,content={
            "status": 422,
            "message": "Episode number exceed available count"
        })
    if not info["external_id"]:
        return JSONResponse(status_code=404,content={
            "status": 404,
            "message": "No external id found"
        })
    if int(episode)<=0:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message": "Episode count cannot be zero or below"
        })

    async def fetch_download_result():
        vault_data = await get_vault_links(id, int(episode), quality)
        if vault_data:
            vault_data["direct_link"] = proxied_download_url(vault_data.get("direct_link"))
            vault_data["snapshot"] = info.get("poster", "")
            return vault_data

        search_result = await get_episode_session(info["external_id"], None)
        episode_info = search_result[int(episode)-1]
        episode_session = episode_info.get("session")
        episode_snapshot = info.get("poster", "")
        results = await get_download_link_from_stream(
            info["external_id"],
            episode_session,
            episode,
            quality,
            info.get("title"),
            episode_snapshot,
            None,
        )
        if not results:
            raise SkipRouteCache(JSONResponse(status_code=500,content={
                "status": 500,
                "message": "Internal error: no results returned"
            }))
        if results.get("status") != 200:
            raise SkipRouteCache(JSONResponse(
                status_code=500 if results.get("status") == 500 else 200,
                content=results
            ))

        return results

    cache_key = f"anime_download:{id}:{episode}:{quality}"
    try:
        return await route_cache.get_or_set(cache_key, DOWNLOAD_CACHE_TTL, fetch_download_result)
    except SkipRouteCache as exc:
        return exc.response

@router.get("/bulk-download", description="Bulk download multiple anime episodes", summary="Bulk download anime episodes")
async def anime_bulk_download(
    request: Request,
    id: str = Query(..., description="ID for the anime from search", examples="OP-1nOtczCOyfuLSUb1ubyydwOFjqOFcvTs74WsZnzMMDck0nU94"),
    ep_from: int = Query(..., alias="from", description="Starting episode number", examples=1, ge=1),
    ep_to: int = Query(..., alias="to", description="Ending episode number", examples=24, ge=1),
    quality: str = Query("720p", regex="^(360p|720p|1080p)$"),
    db = Depends(get_db),
): 

    if ep_from > ep_to:
        return JSONResponse(status_code=400, content={
            "status": 400,
            "message": "Starting episode cannot be greater than ending episode"
        })
    total_ep_count = (ep_to - ep_from) + 1
    episodes = await get_actual_episode_count(id, db)
    info = await get_cached_anime_info(id, None)
    if episodes is None:
        return JSONResponse(
            status_code=500,
            content={
                "status":500,
                "message":"Episode is none due to bad internet connection"
            }
        )
    if episodes > 100:
        base_limit = 50
    else:
        base_limit = max(1, episodes // 2)

    ep_limit = base_limit
    if total_ep_count > ep_limit:
        message = f"Limit reached. You can only download a maximum of {ep_limit} episodes at a time for {info['title']}."
    
        return JSONResponse(
            status_code=400,
            content={
            "status": 400,
            "message": message
        }
    )
    # Get anime info
    if not info.get("status") == 200:
        return JSONResponse(
            status_code=info.get("status"),
            content={**info}
        )
    ep_count = info["episodes"]
    # Check if episodes are within range
    if ep_to > int(ep_count) or ep_from > int(ep_count):
        return JSONResponse(status_code=422, content={
            "status": 422,
            "episodes": ep_count,
            "message": "Episode number exceeds available count"
        })
    
    if not info["external_id"]:
        return JSONResponse(status_code=404, content={
            "status": 404,
            "message": "No external id found"
        })
    
    # Create list of episode numbers to fetch
    episodes = list(range(ep_from, ep_to + 1))
    bulk_links_cache_key = f"anime_bulk_links:{id}:{ep_from}:{ep_to}:{quality}"

    async def fetch_bulk_links():
        download_links = await asyncio.gather(*[
            _fetch_single_episode(id, episode, info["external_id"], None, quality)
            for episode in episodes
        ])
        return [link for link in download_links if link is not None]

    successful_links = await route_cache.get_or_set(
        bulk_links_cache_key,
        DOWNLOAD_CACHE_TTL,
        fetch_bulk_links
    )
    
    if not successful_links:
        return JSONResponse(status_code=500, content={
            "status": 500,
            "message": "Failed to fetch any episode links"
        })
    
    # CREATE SESSION - Store links in memory
    session_id = str(uuid.uuid4())
    poster = info.get("poster") or info.get("image_url") or "https://files.catbox.moe/zqjntf.png"
    await DOWNLOAD_SESSION_CACHE.set(
        session_id,
        {
            "session_id": session_id,
            "anime_id": id,
            "anime_title": info.get("title", "Unknown"),
            "links": successful_links,
            "poster": poster,
            "created_at": time.time(),
        },
        60 * 60 * 2,
    )
    
    
    return JSONResponse(status_code=200, content={
        "status": 200,
        "session_id": session_id,  # NEW: Return session ID
        "anime_title": info.get("title", "Unknown"),
        "total_requested": len(episodes),
        "total_fetched": len(successful_links),
        "links": successful_links
    })

async def _fetch_single_episode(id: str, episode: int, external_id: str, db, quality):
    """Helper function to fetch a single episode link"""
    try:
        vault_data = await get_vault_links(id, episode, quality)
        if vault_data:
            # Grab poster for UI consistency
            anime_info = await get_cached_anime_info(id, db)
            vault_data["direct_link"] = proxied_download_url(vault_data.get("direct_link"))
            vault_data["snapshot"] = anime_info.get("poster", "")
            vault_data["episode_label"] = episode
            print(f"📦 Episode {episode}: Served instantly from Vault")
            return vault_data
        
        # Fetch fresh link
        await asyncio.sleep(0.5)
        
        search_result = await get_episode_session(external_id, None)
        episode_info = search_result[episode - 1]
        episode_session = episode_info.get("session")
        anime_info = await get_cached_anime_info(id, None)
        episode_snapshot = anime_info.get("poster", "")

        results = await get_download_link_from_stream(
            external_id,
            episode_session,
            episode,
            quality,
            anime_info.get("title"),
            episode_snapshot,
            None,
        )
        
        if results and results.get("status") == 200:
            results["episode_label"] = episode
            return results
        else:
            print(f"❌ Episode {episode}: Failed to get redirect link")
            return None
            
    except Exception as e:
        print(f"❌ Episode {episode}: Error - {e}")
        import traceback
        traceback.print_exc()
        return None

import os
import json
import asyncio
import tempfile
import time
import re
from datetime import datetime, timedelta,timezone
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
import aiosqlite
from fastapi import BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# ============================================
# V3.0 GLOBAL STATE (The Status Memory)
# ============================================
ACTIVE_DOWNLOADS = {}

# ============================================
# HTTP Endpoint: Start the Download Task
# ============================================
@router.post("/bulk-download/start/{session_id}")
async def start_bulk_download(
    session_id: str, 
    background_tasks: BackgroundTasks, 
    db = Depends(get_db)
):
    """Starts the download process in the background."""
    
    # 1. Prevent duplicate tasks if user clicks twice
    if session_id in ACTIVE_DOWNLOADS and ACTIVE_DOWNLOADS[session_id].get("status") not in ["error", "complete", "episode_failed"]:
        return {"status": "already_running", "session_id": session_id}

    # 2. Fetch session data from memory
    row = await DOWNLOAD_SESSION_CACHE.get(session_id)

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
        
    # 3. Initialize the global status
    links = row["links"]
    ACTIVE_DOWNLOADS[session_id] = {
        "status": "started",
        "message": f"Preparing to download {len(links)} episodes...",
        "progress": 0,
        "total_episodes": len(links)
    }

    # 4. Hand off the heavy lifting to FastAPI BackgroundTasks
    # We pass row dict so we don't pass the soon-to-be-closed db connection
    background_tasks.add_task(process_bulk_download, session_id, row)
    
    return {"status": "started", "session_id": session_id}

# ============================================
# HTTP Endpoint: Check Status (Polling)
# ============================================
@router.get("/bulk-download/status/{session_id}")
@limiter.exempt
async def get_download_status(session_id: str, request: Request):
    """Frontend calls this every 2 seconds to get the live progress."""
    if session_id not in ACTIVE_DOWNLOADS:
        return JSONResponse(
            status_code=404,
            content={"status": "not_found", "message": "No active download found."}
        )

    data = dict(ACTIVE_DOWNLOADS[session_id])  # copy so we don't mutate global state

    # If complete, calculate fresh remaining seconds so the frontend
    # gets the true value regardless of when the user opens the tab
    if data.get("status") == "complete" and data.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(data["expires_at"]).astimezone(timezone.utc)
            remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())
            data["expires_in_seconds"] = max(remaining, 0)

            # If it's already expired, tell the frontend
            if remaining <= 0:
                data["status"] = "expired"
                data["message"] = "This download has expired."
        except Exception as e:
            print(e)
            data["expires_in_seconds"] = None

    return data
# ============================================
# MODIFIED: process_bulk_download
# Changes:
#   1. Store temp_dir in ACTIVE_DOWNLOADS so cancel route can clean it up
#   2. Check cancelled flag between episodes
#   3. Fix expires_at to use UTC consistently
# ============================================
async def process_bulk_download(session_id: str, row_data: dict):
    temp_dir = tempfile.mkdtemp()

    try:
        links = row_data["links"]
        raw_title = row_data["anime_title"]
        anime_title_clean = re.sub(r'[\\/:*?"<>|]', '_', raw_title).replace(" ", "_").lower()
        anime_title = f"K-[ANIME.ME]_{anime_title_clean}"

        episodes = [int(link_info.get("episode")) for link_info in links if link_info.get("episode")]
        from_ep, to_ep = (min(episodes), max(episodes)) if episodes else (1, 1)
        zip_filename = f"{anime_title}_{from_ep}-{to_ep}_episodes.zip"

        zip_path = os.path.join(temp_dir, zip_filename)
        successful_episodes = []

        # Store temp_dir in RAM so the cancel route can wipe it
        ACTIVE_DOWNLOADS[session_id]["temp_dir"] = temp_dir

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
        ) as client:

            for idx, link_info in enumerate(links, 1):

                # ── Check cancel flag before each episode ──────────────────
                if ACTIVE_DOWNLOADS[session_id].get("cancelled"):
                    print(f"🛑 Download cancelled for session {session_id} before episode {link_info.get('episode')}")
                    return

                episode = link_info.get("episode")
                url = link_info.get("direct_link")
                if not url:
                    continue

                temp_file = os.path.join(temp_dir, f"ep_{episode}.mp4")

                ACTIVE_DOWNLOADS[session_id].update({
                    "status": "downloading",
                    "episode": episode,
                    "current": idx,
                    "message": f"Downloading Episode {episode}...",
                    "progress": 0
                })

                success = await download_with_retry_http(
                    client, url, temp_file, episode, session_id, idx, len(links)
                )

                # ── Check cancel flag after each episode too ───────────────
                if ACTIVE_DOWNLOADS[session_id].get("cancelled"):
                    print(f"🛑 Download cancelled for session {session_id} after episode {episode}")
                    return

                if success:
                    ep_label = link_info.get("episode_label", str(episode))
                    successful_episodes.append({
                        'episode': episode,
                        'temp_file': temp_file,
                        'filename': f"{anime_title}_Episode_{str(ep_label).zfill(3)}.mp4"
                    })
                else:
                    ACTIVE_DOWNLOADS[session_id].update({
                        "status": "episode_failed",
                        "message": f"❌ Episode {episode} failed after retries."
                    })
                    if os.path.exists(temp_file):
                        os.remove(temp_file)

        if not successful_episodes:
            ACTIVE_DOWNLOADS[session_id].update({
                "status": "error",
                "message": "No episodes downloaded successfully!"
            })
            return

        # ZIP Process
        ACTIVE_DOWNLOADS[session_id].update({
            "status": "zipping",
            "message": f"Creating ZIP file with {len(successful_episodes)} episodes..."
        })

        with ZipFile(zip_path, 'w', ZIP_DEFLATED) as zipf:
            for ep_idx, ep_info in enumerate(successful_episodes, 1):

                # ── Check cancel flag between ZIP entries too ──────────────
                if ACTIVE_DOWNLOADS[session_id].get("cancelled"):
                    print(f"🛑 Download cancelled during zipping for session {session_id}")
                    return

                zipf.write(ep_info['temp_file'], ep_info['filename'])
                ACTIVE_DOWNLOADS[session_id].update({
                    "message": f"Adding Episode {ep_info['episode']} to ZIP...",
                    "zip_progress": int((ep_idx / len(successful_episodes)) * 100)
                })
                os.remove(ep_info['temp_file'])

        zip_size = os.path.getsize(zip_path)
        archive_quality = next((link.get("quality") for link in links if link.get("quality")), "720p")

        # Use UTC consistently to avoid timezone offset bugs
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await DOWNLOAD_ZIP_CACHE.set(
            session_id,
            {
                "session_id": session_id,
                "zip_path": zip_path,
                "expires_at": expires_at.isoformat(),
            },
            60 * 60,
        )

        # Final Success State
        ACTIVE_DOWNLOADS[session_id].update({
            "status": "complete",
            "message": "ZIP file ready for download!",
            "download_url": f"/anime/download-zip/{session_id}",
            "filename": zip_filename,
            "size_mb": round(zip_size / 1024 / 1024, 2),
            "expires_at": expires_at.isoformat(),  # RAM only — for countdown
        })

    except Exception as e:
        print(f"❌ Background task error: {e}")
        ACTIVE_DOWNLOADS[session_id].update({"status": "error", "message": str(e)})
        # Clean up temp dir on error too
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


# ============================================
# MODIFIED: download_with_retry_http
# Change: check cancelled flag between chunks
# ============================================
async def download_with_retry_http(client, url, temp_file, episode, session_id, current, total, max_retries=3):
    total_size = None

    for attempt in range(max_retries):
        try:
            start_byte = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
            
            # ---> FIX 1: Clean up the headers <---
            headers = {'User-Agent': 'Mozilla/5.0'}
            # Only use the Kwik referer if it's NOT our Hugging Face vault
            if "huggingface.co" not in url:
                headers['Referer'] = 'https://kwik.cx/'
                
            if start_byte > 0:
                headers['Range'] = f'bytes={start_byte}-'

            start_time = time.time()

            # ---> FIX 2: Add follow_redirects=True <---
            async with client.stream('GET', url, headers=headers, timeout=120.0, follow_redirects=True) as response:
                if start_byte > 0 and response.status_code not in [206, 200]:
                    start_byte = 0
                    if os.path.exists(temp_file):
                        os.remove(temp_file)

                response.raise_for_status()
                content_length = response.headers.get('content-length')

                if start_byte > 0 and response.status_code == 206:
                    total_size = start_byte + int(content_length) if content_length else None
                    mode = 'ab'
                    downloaded = start_byte
                else:
                    total_size = int(content_length) if content_length else None
                    mode = 'wb'
                    downloaded = 0
                    start_byte = 0

                with open(temp_file, mode) as f:
                    last_update = time.time()

                    async for chunk in response.aiter_bytes(chunk_size=1024*1024):

                        # ── Check cancel flag between every chunk ──────────
                        if ACTIVE_DOWNLOADS[session_id].get("cancelled"):
                            print(f"🛑 Chunk download cancelled mid-stream for session {session_id}")
                            # Delete the partial file
                            f.close() if not f.closed else None
                            if os.path.exists(temp_file):
                                os.remove(temp_file)
                            return False

                        f.write(chunk)
                        downloaded += len(chunk)

                        if time.time() - last_update >= 0.5:
                            progress = min(int((downloaded / total_size) * 100), 100) if total_size else 0
                            elapsed = time.time() - start_time
                            speed = (downloaded - start_byte) / elapsed if elapsed > 0 else 0

                            ACTIVE_DOWNLOADS[session_id].update({
                                "progress": progress,
                                "downloaded_mb": round(downloaded / 1024 / 1024, 2),
                                "total_mb": round(total_size / 1024 / 1024, 2) if total_size else None,
                                "speed_mbps": round(speed / 1024 / 1024, 2),
                                "message": f"Downloading Episode {episode}... {progress}%",
                                "attempt": attempt + 1
                            })
                            last_update = time.time()

            return True

        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                ACTIVE_DOWNLOADS[session_id].update({
                    "status": "retrying",
                    "message": f"Connection lost! Retry {attempt + 1}/{max_retries} in {wait_time}s..."
                })
                await asyncio.sleep(wait_time)
            else:
                print(f"❌ Final download failure for Episode {episode}: {e}")
                return False

    return False
# ============================================
# NEW ROUTE: Cancel / Delete a download
# ============================================
@router.delete("/bulk-download/cancel/{session_id}")
async def cancel_bulk_download(session_id: str):
    """
    Sets a cancel flag so the background worker stops gracefully.
    Also cleans up any temp files already on disk.
    """
    if session_id not in ACTIVE_DOWNLOADS:
        # Already gone — that's fine, just return success
        return {"status": "cancelled", "session_id": session_id}

    current_status = ACTIVE_DOWNLOADS[session_id].get("status")

    # If already terminal, nothing to cancel — just clean up RAM
    if current_status in ["complete", "error", "episode_failed", "expired"]:
        ACTIVE_DOWNLOADS.pop(session_id, None)
        return {"status": "cancelled", "session_id": session_id}

    # Signal the background worker to stop
    ACTIVE_DOWNLOADS[session_id]["cancelled"] = True
    ACTIVE_DOWNLOADS[session_id]["status"] = "cancelled"
    ACTIVE_DOWNLOADS[session_id]["message"] = "Download cancelled by user."

    # Clean up temp dir if it was stored
    temp_dir = ACTIVE_DOWNLOADS[session_id].get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            print(f"⚠️ Could not delete temp dir {temp_dir}: {e}")

    return {"status": "cancelled", "session_id": session_id}


# ============================================
# Download endpoint - retrieves from database
# ============================================
@router.get("/download-zip/{session_id}")
async def download_completed_zip(
    session_id: str,
    db = Depends(get_db)
):
    """
    Simple endpoint to download the already-prepared ZIP file
    This is called AFTER the WebSocket completes
    """
    # Get ZIP path from memory cache
    row = await DOWNLOAD_ZIP_CACHE.get(session_id)
    
    if not row:
        return JSONResponse(
            status_code=404, 
            content={"status": 404, "message": "ZIP file not found or expired"}
        )
    
    zip_path = row["zip_path"]
    expires_at = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc)
    
    # Check if expired
    if datetime.now(timezone.utc) > expires_at:
        # Clean up expired file
        if os.path.exists(zip_path):
            os.remove(zip_path)
        await DOWNLOAD_ZIP_CACHE.delete(session_id)
        return JSONResponse(
            status_code=410, 
            content={"status": 410, "message": "ZIP file expired"}
        )
    
    # Check if file exists
    if not os.path.exists(zip_path):
        await DOWNLOAD_ZIP_CACHE.delete(session_id)
        return JSONResponse(
            status_code=404, 
            content={"status": 404, "message": "ZIP file not found"}
        )
    
    # Get filename from path
    filename = os.path.basename(zip_path)
    
    print(f"📥 Serving ZIP: {filename} ({os.path.getsize(zip_path) / 1024 / 1024:.2f} MB)")
    
    # Don't delete immediately - let user download
    # Schedule cleanup for later (you can add a cleanup cron job)
    print(quote(filename))
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{quote(filename)}"'
        }
    )



# ============================================
# Optional: Cleanup endpoint (run as cron job)
# ============================================
@router.post("/cleanup-expired-zips")
async def cleanup_expired_zips(db = Depends(get_db)):
    """
    Cleanup expired ZIP files (run this as a scheduled task)
    """
    rows = await DOWNLOAD_ZIP_CACHE.snapshot()
    
    cleaned = 0
    now = datetime.now(timezone.utc)
    for session_id, row in rows.items():
        zip_path = row["zip_path"]
        expires_at_raw = row.get("expires_at")
        try:
            expires_at = datetime.fromisoformat(expires_at_raw).astimezone(timezone.utc) if expires_at_raw else now
        except Exception:
            expires_at = now

        if expires_at >= now:
            continue
        
        if os.path.exists(zip_path):
            try:
                temp_dir = os.path.dirname(zip_path)
                shutil.rmtree(temp_dir, ignore_errors=True)
                cleaned += 1
                print(f"🧹 Cleaned up: {zip_path}")
            except Exception as e:
                print(f"Failed to cleanup {zip_path}: {e}")
        
        await DOWNLOAD_ZIP_CACHE.delete(session_id)
    
    return {"status": 200, "message": f"Cleaned up {cleaned} expired files"}


@router.get("/kwik.m3u8")
async def get_m3u8_playlist(
    url: str = Query(...)):
    if not url:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message":"Url is required"
        })
        
    async with httpx.AsyncClient(headers=kwik_page_headers) as client:
        response = await client.get(url)
    
        html_content = response.text
    m3u8_link = await extract_kwik_m3u8(html_content)
    proxied_m3u8 = await get_proxied_m3u8(m3u8_link)
    if not proxied_m3u8:
        return JSONResponse(status_code=500, content={"error": "Could not fetch playlist"})
        
    # CRITICAL: This header tells the browser's video player to treat this text as an HLS stream
    return Response(
        content=proxied_m3u8, 
        media_type="application/vnd.apple.mpegurl"
    )

@router.get("/airing-now",description="Get airing animes in the same format as search")
async def get_airing_now(page: int = Query(description="Airing animes per page", exampless=1), db=Depends(get_db)):
    try:
        async def fetch_airing_page():
            airing_anime = []
            cookies = await get_animepahe_cookies(db)
            async with httpx.AsyncClient(cookies=cookies, timeout=60) as client:
                res = await client.get(f"https://animepahe.pw/api?m=airing")
                check = res.json()
                if check.get("last_page") < page:
                    return {
                        "status":404,
                        "current_page": page,
                        "last_page":check.get("last_page"),
                        "data":[]
                    }
                res = await client.get(f"https://animepahe.pw/api?m=airing&page={page}")
                try:
                    results = res.json()
                except ValueError:
                    print("❌ Not a JSON response:", res.text[:200])  # show first part of the response for debugging
                    raise ValueError("Invalid JSON response from AnimePahe airing list")

            last_page = results.get("last_page")
            info = results.get("data")
            for i in info:
                cursor = await db.execute(
                    "SELECT internal_id FROM anime_info WHERE external_id = ?", (i.get("anime_id"),))
                row = await cursor.fetchone()
                episodes = await get_actual_episode_count(i.get("anime_id"),db)
                status = "Finished Airing" if i.get("completed") == 1 else "Currently Airing"
                if not row:
                    await db.execute('''
                    INSERT INTO anime_info(internal_id, external_id, title, episodes,poster)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        title = excluded.title,
                        episodes = excluded.episodes,
                        poster = excluded.poster;

                    ''',
                            (i.get("anime_id"), i.get("anime_id"), i.get("anime_title"), episodes or "??",i.get("snapshot")))
                    await db.commit()
                else:
                    internal_id = row["internal_id"]
                filtered_airing_anime = {
                    "id": i.get("anime_id"),
                    "title": i.get("anime_title"),
                    "episodes": episodes,
                    "status": status,
                    "poster":i.get("snapshot"),
                    "created_at":i.get("created_at")
                }
                airing_anime.append(filtered_airing_anime)
            return {
                        "status":200,
                        "current_page": page,
                        "last_page":last_page,
                        "data": airing_anime
            }

        cache_key = f"airing_now:{page}"
        result = await route_cache.get_or_set(cache_key, AIRING_NOW_CACHE_TTL, fetch_airing_page)
        if result.get("status") == 404:
            return JSONResponse(status_code=404, content=result)
        return result
    except Exception as e:
        print("Airing anime error: ",e)
        traceback.print_exc()
        return JSONResponse(status_code=500,content={
            "status":500,
            "message":"Internal Server error"
        })


@router.get("/{internal_id}", description="Get anime info based on the id")
async def get_anime_info(
    internal_id: str,
    page: int = Query(1, ge=1, description="Page number for episodes"),
    db = Depends(get_db)
):
    try:
        mal_id = await get_mal_id(internal_id, db)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MAL ID: {str(e)}")

    async with httpx.AsyncClient() as client:
        cookies = await get_animepahe_cookies(db)

        jikan_task = client.get(f"https://api.jikan.moe/v4/anime/{mal_id}", timeout=10.0)
        external_id =  await get_external_id(client, internal_id, cookies)
        print(external_id)
        pahe_task = client.get(
            f"https://animepahe.pw/api?m=release&id={external_id}&sort=episode_desc&page={page}",
            timeout=10.0,
            cookies=cookies
        )

        # Use return_exceptions so one failure doesn't kill the other
        jikan_result, pahe_result = await asyncio.gather(jikan_task, pahe_task, return_exceptions=True)

        # Pahe is required — no episodes = no point
        if isinstance(pahe_result, Exception):
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"Failed to fetch episode data: {str(pahe_result)}")
        # Handle HTTP errors properly
        if pahe_result.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Anime episodes not found (invalid id or no release data)"
            )

        try:
            pahe_result.raise_for_status()
            episode_data = pahe_result.json()
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Upstream anime service error: {str(e)}"
            )

        # Jikan is optional — graceful fallback
        anime_data = None
        if not isinstance(jikan_result, Exception):
            try:
                jikan_result.raise_for_status()
                anime_data = jikan_result.json().get('data')
            except Exception:
                traceback.print_exc()
                # anime_data stays None, we'll use barebone fallback below

    episodes = []
    all_episodes = await get_episode_session(internal_id, db)
    session_to_index = {ep["session"]: idx + 1 for idx, ep in enumerate(all_episodes)}

    for ep in episode_data.get('data', []):
        actual_episode = session_to_index.get(ep['session'], 0)
        episodes.append({
            "episode_number": actual_episode,
            "duration": ep['duration'],
            "is_filler": bool(ep['filler']),
            "snapshot": ep['snapshot'],
        })

    # Build response — Jikan fields fall back to None if unavailable
    broadcast_day = None
    if anime_data:
        if anime_data.get('status') == "Currently Airing" and anime_data.get('broadcast'):
            broadcast_day = anime_data['broadcast'].get('day')

    return {
        "id": internal_id,
        "synopsis": anime_data.get('synopsis', '') if anime_data else None,
        "image_url": anime_data['images']['jpg']['large_image_url'] if anime_data else None,
        "genres": [genre['name'] for genre in anime_data.get('genres', [])] if anime_data else [],
        "duration": anime_data.get('duration', '') if anime_data else None,
        "rating": anime_data.get('rating', '') if anime_data else None,
        "score": anime_data.get('score') if anime_data else None,
        "title": (anime_data.get("title_english") or anime_data.get("title")) if anime_data else None,
        "year": anime_data.get('year') if anime_data else None,
        "broadcast_day": broadcast_day,
        "status": anime_data.get('status', '') if anime_data else None,
        "episodes": episodes,
        "pagination": {
            "current_page": episode_data.get('current_page', page),
            "last_page": episode_data.get('last_page', 1),
            "per_page": episode_data.get('per_page', 30),
            "total": episode_data.get('total', 0)
        }
    }

@router.get("/stream/{internal_id}/{episode_number}")
async def stream_episode(
    internal_id: str,
    episode_number: int,
    db = Depends(get_db)
):
    # Get ALL episodes to find the session hash
    all_episodes = await get_episode_session(internal_id, db)
    
    # Find the episode session for this episode number
    episode_session = None
    for ep in all_episodes:
        if ep['episode'] == episode_number:
            episode_session = ep['session']
            break
    
    if not episode_session:
        raise HTTPException(404, "Episode not found")

    stream_links = await get_stream_links(internal_id, episode_session, db)
    # Now scrape the .m3u8 from the player page
    # m3u8_url = await get_m3u8_url(session_id, episode_session)
    info = {
        "status":200,
        "results": stream_links
    }
    return info

@router.get("/session/{session_id}")
async def get_download_session(session_id: str, db=Depends(get_db)):
    row = await DOWNLOAD_SESSION_CACHE.get(session_id)
    
    if not row:
        return JSONResponse(status_code=404, content={
            "status": 404,
            "message": "Session not found or expired"
        })
    return JSONResponse(status_code=200, content={
        "status": 200,
        "anime_id":row["anime_id"],
        "poster": row["poster"],
        "anime_title": row["anime_title"],
        "links": row["links"]
    })
