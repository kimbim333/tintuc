"""
Bot tổng hợp tin tức hàng ngày -> AI tóm tắt (Gemini, miễn phí) -> gửi qua Telegram
Nguồn: RSS chính thức của các báo uy tín
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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

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

VN_TZ = timezone(timedelta(hours=7))


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
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[LỖI] Không tải được {name}: {e}")
        return items

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)

    for entry in feed.entries:
        pub_time = parse_entry_time(entry)
        if pub_time and pub_time < cutoff:
            continue
        title = clean_html(entry.get("title", "")).strip()
        summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
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


def build_raw_digest_text(all_by_source: dict) -> str:
    """Chuyển toàn bộ tin thô thành 1 khối văn bản để đưa cho Gemini xử lý."""
    lines = []
    for source, items in all_by_source.items():
        lines.append(f"\n## Nguồn: {source}")
        for item in items:
            lines.append(f"- Tiêu đề: {item['title']}")
            if item["summary"]:
                lines.append(f"  Mô tả gốc: {item['summary']}")
            lines.append(f"  Link: {item['link']}")
    return "\n".join(lines)


def summarize_with_gemini(raw_text: str, today: str) -> str:
    """Gọi Gemini (miễn phí) để tóm tắt lại toàn bộ tin thành 1 bản tin gọn gàng."""
    prompt = f"""Bạn là biên tập viên tin tức. Dưới đây là danh sách tin thô lấy từ RSS của nhiều báo Việt Nam uy tín, sáng ngày {today}.

Hãy tổng hợp lại thành một BẢN TIN SÁNG đầy đủ, với yêu cầu:
1. ƯU TIÊN GIỮ SỐ LƯỢNG TIN ĐA DẠNG NHIỀU NHẤT CÓ THỂ. CHỈ gộp 2 tin làm 1 khi chúng chắc chắn nói về CÙNG MỘT sự kiện/sự việc cụ thể (cùng địa điểm, cùng nhân vật, cùng thời điểm xảy ra) mà nhiều báo cùng đưa tin. Nếu chỉ là 2 tin CÙNG CHỦ ĐỀ chung chung (ví dụ cùng nói về giá vàng nhưng khác ngày, khác số liệu, khác góc độ) thì phải giữ RIÊNG, không được gộp. Khi không chắc chắn có phải cùng 1 sự kiện hay không, hãy giữ riêng thay vì gộp.
2. Không tự ý bỏ bớt tin để cho bản tin "gọn" — giữ lại toàn bộ các tin không trùng lặp, kể cả tin nhỏ.
3. Mỗi tin: viết lại tiêu đề ngắn gọn, rõ ràng + tóm tắt nội dung bằng 1-2 câu văn tự nhiên, khách quan, dễ hiểu (không sao chép nguyên văn mô tả gốc).
4. Nhóm theo chủ đề lớn (Thời sự - Xã hội, Kinh tế, Thế giới, Giải trí - Đời sống, Công nghệ, Khác...) thay vì nhóm theo tên báo.
5. Cuối mỗi tin, giữ lại đúng 1 link nguồn (chọn link từ báo nào tin đó xuất hiện đầu tiên; nếu gộp nhiều báo thì chọn 1 link đại diện).
6. Định dạng bằng Markdown đơn giản của Telegram: dùng *chữ đậm* cho tiêu đề mục và tiêu đề từng chủ đề, KHÔNG dùng bảng, KHÔNG dùng tiêu đề kiểu #.
7. Viết bằng tiếng Việt, giọng văn trung lập, không giật tít.
8. Bắt đầu bản tin bằng dòng: *🗞️ BẢN TIN SÁNG {today}*

Dữ liệu tin thô:
{raw_text}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 8192,
        },
    }

    resp = requests.post(GEMINI_URL, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


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
            # Nếu markdown bị lỗi định dạng, gửi lại dạng văn bản thường
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
        raw_text = build_raw_digest_text(all_by_source)
        try:
            digest = summarize_with_gemini(raw_text, today)
        except Exception as e:
            print(f"[LỖI Gemini] {e}")
            digest = f"*🗞️ BẢN TIN SÁNG {today}*\n\n(AI tóm tắt lỗi, gửi tạm tin thô)\n\n{raw_text[:3000]}"
        send_telegram_message(digest)

    print("Đã gửi bản tin thành công.")
