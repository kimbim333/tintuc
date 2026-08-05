"""
Bot tổng hợp tin tức hàng ngày -> AI tóm tắt (Groq, miễn phí) -> gửi qua Telegram
Nguồn: RSS chính thức của các báo uy tín

Do free tier của Groq giới hạn token/phút khá thấp cho mỗi request, việc tóm tắt
được chia làm 2 giai đoạn để tránh lỗi "request quá lớn":
  Giai đoạn 1: tóm tắt gọn từng báo riêng lẻ (nhiều request nhỏ)
  Giai đoạn 2: gộp toàn bộ bản tóm tắt gọn đó lại, lọc trùng, viết bản tin cuối
"""

import os
import time
import html
import re
import requests
import feedparser
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Các nguồn RSS uy tín, mục "tin mới nhất" = tất cả chủ đề
FEEDS = {
    "VnExpress": "https://vnexpress.net/rss/tin-moi-nhat.rss",
    "Tuổi Trẻ": "https://tuoitre.vn/rss/tin-moi-nhat.rss",
    "Thanh Niên": "https://thanhnien.vn/rss/home.rss",
    "Dân Trí": "https://dantri.com.vn/rss/home.rss",
    "Lao Động": "https://laodong.vn/rss/home.rss",
    "Kenh14": "https://kenh14.vn/rss/home.rss",
    "Hà Nội Mới": "https://hanoimoi.vn/rss/trang-chu",
}

MAX_ITEMS_PER_SOURCE = 15
HOURS_LOOKBACK = 20  # lấy tin trong khoảng 20h gần nhất (chạy hàng ngày lúc 6h sáng)
MAX_SUMMARY_CHARS = 180  # cắt bớt mô tả gốc quá dài để tiết kiệm token

VN_TZ = timezone(timedelta(hours=7))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def clean_html(raw_html: str) -> str:
    """Loại bỏ thẻ HTML còn sót lại trong mô tả RSS."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", "", raw_html)
    text = html.unescape(text)
    return text.strip()


def parse_entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        if getattr(entry, key, None):
            return datetime(*getattr(entry, key)[:6], tzinfo=timezone.utc)
    return None


def fetch_source(name: str, url: str):
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"[LỖI] Không tải được {name}: {e}")
        return items

    if not feed.entries:
        print(f"[CẢNH BÁO] {name} trả về 0 tin (có thể bị chặn hoặc feed rỗng). Status: {getattr(feed, 'status', 'N/A')}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)

    for entry in feed.entries:
        pub_time = parse_entry_time(entry)
        if pub_time and pub_time < cutoff:
            continue
        title = clean_html(entry.get("title", "")).strip()
        summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "..."
        link = entry.get("link", "")
        if not title:
            continue
        items.append({"title": title, "summary": summary, "link": link})
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break

    return items


def collect_all_items():
    """Lấy toàn bộ tin thô từ tất cả các nguồn, gộp theo báo."""
    all_by_source = {}
    for source, url in FEEDS.items():
        items = fetch_source(source, url)
        if items:
            all_by_source[source] = items
    return all_by_source


def build_source_text(source: str, items: list) -> str:
    lines = [f"Nguồn: {source}"]
    for item in items:
        lines.append(f"- Tiêu đề: {item['title']}")
        if item["summary"]:
            lines.append(f"  Mô tả gốc: {item['summary']}")
        lines.append(f"  Link: {item['link']}")
    return "\n".join(lines)


def call_groq(prompt: str, max_tokens: int) -> str:
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(4):
        resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=90)
        if resp.status_code in (429, 413):
            wait = 20 * (attempt + 1)
            print(f"[Groq] Lỗi {resp.status_code}, thử lại sau {wait}s... ({resp.text[:200]})")
            last_error = requests.HTTPError(f"{resp.status_code}: {resp.text}")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    raise last_error


def summarize_source(source: str, items: list) -> str:
    """Giai đoạn 1: tóm tắt gọn các tin của 1 báo (request nhỏ, tránh vượt giới hạn token/phút)."""
    raw = build_source_text(source, items)
    prompt = f"""Dưới đây là danh sách tin thô lấy từ RSS của báo {source}.

Hãy viết lại thành danh sách các mục tin GỌN, mỗi tin theo đúng định dạng sau (không thêm gì khác):
[Nhãn chủ đề ngắn] Tiêu đề rút gọn -- Tóm tắt 1 câu ngắn, khách quan -- Link

Yêu cầu:
- Giữ TOÀN BỘ số lượng tin, không bỏ tin nào, kể cả tin nhỏ.
- "Nhãn chủ đề" chọn 1 trong: Thời sự, Xã hội, Kinh tế, Thế giới, Giải trí, Đời sống, Công nghệ, Thể thao, Khác.
- Không sao chép nguyên văn mô tả gốc, phải viết lại bằng lời văn riêng.
- Viết bằng tiếng Việt.

Dữ liệu:
{raw}
"""
    return call_groq(prompt, max_tokens=1400)


def merge_final_digest(distilled_texts: list, today: str) -> str:
    """Giai đoạn 2: gộp tất cả tin đã tóm tắt gọn, lọc trùng, viết bản tin cuối."""
    combined = "\n\n".join(distilled_texts)
    prompt = f"""Dưới đây là danh sách tin tức đã được tóm tắt gọn từ nhiều báo Việt Nam uy tín, sáng ngày {today}. Mỗi dòng có định dạng: [Chủ đề] Tiêu đề -- Tóm tắt -- Link

Hãy tổng hợp lại thành một BẢN TIN SÁNG hoàn chỉnh, với yêu cầu:
1. ƯU TIÊN GIỮ SỐ LƯỢNG TIN ĐA DẠNG NHIỀU NHẤT CÓ THỂ. CHỈ gộp 2 tin làm 1 khi chúng chắc chắn nói về CÙNG MỘT sự kiện cụ thể (cùng địa điểm, cùng nhân vật, cùng thời điểm) mà nhiều báo cùng đưa. Nếu chỉ cùng chủ đề chung chung thì giữ riêng. Khi không chắc, giữ riêng.
2. Không tự ý bỏ bớt tin để "gọn" — giữ toàn bộ tin không trùng lặp.
3. Nhóm theo đúng nhãn chủ đề đã có sẵn (Thời sự - Xã hội, Kinh tế, Thế giới, Giải trí - Đời sống, Công nghệ, Thể thao, Khác).
4. Mỗi tin giữ đúng 1 link (nếu gộp nhiều báo thì chọn 1 link đại diện).
5. Định dạng Markdown đơn giản của Telegram: dùng *chữ đậm* cho tiêu đề mục/chủ đề, KHÔNG dùng bảng, KHÔNG dùng tiêu đề kiểu #.
6. Viết bằng tiếng Việt, giọng văn trung lập.
7. Bắt đầu bằng dòng: *🗞️ BẢN TIN SÁNG {today}*

Dữ liệu:
{combined}
"""
    return call_groq(prompt, max_tokens=3800)


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    max_len = 3800

    def _send(chunk, use_markdown):
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if use_markdown:
            payload["parse_mode"] = "Markdown"
        return requests.post(url, data=payload, timeout=30)

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    for chunk in chunks:
        resp = _send(chunk, use_markdown=True)
        if not resp.ok:
            print("Lỗi Markdown, gửi lại dạng thường:", resp.text)
            resp = _send(chunk, use_markdown=False)
            if not resp.ok:
                print("Lỗi gửi Telegram:", resp.text)
        time.sleep(1)


if __name__ == "__main__":
    today = datetime.now(VN_TZ).strftime("%d/%m/%Y")
    all_by_source = collect_all_items()

    if not all_by_source:
        send_telegram_message(f"*🗞️ BẢN TIN SÁNG {today}*\n\nKhông có tin mới trong khoảng thời gian này.")
    else:
        distilled_texts = []
        for source, items in all_by_source.items():
            try:
                distilled = summarize_source(source, items)
                distilled_texts.append(distilled)
                print(f"Đã tóm tắt xong nguồn: {source} ({len(items)} tin)")
            except Exception as e:
                print(f"[LỖI Groq - tóm tắt {source}] {e}")
            time.sleep(3)  # nghỉ ngắn giữa các lần gọi để tránh dồn dập

        if not distilled_texts:
            digest = f"*🗞️ BẢN TIN SÁNG {today}*\n\n(AI tóm tắt lỗi toàn bộ, thử lại vào lần chạy sau)"
        else:
            try:
                digest = merge_final_digest(distilled_texts, today)
            except Exception as e:
                print(f"[LỖI Groq - gộp bản tin] {e}")
                digest = f"*🗞️ BẢN TIN SÁNG {today}*\n\n" + "\n\n".join(distilled_texts)

        send_telegram_message(digest)

    print("Đã gửi bản tin thành công.")
