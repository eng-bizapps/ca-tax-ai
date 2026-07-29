import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
# try qcode API
urls = {
 "malibu_api": "https://api.qcode.us/lib/malibu_ca/pub/municipal_code/item/title_5-chapter_5_55-5_55_130",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code, len(r.text))
        print(r.text[:2000])
    except Exception as e:
        print(name, "ERROR", e)
