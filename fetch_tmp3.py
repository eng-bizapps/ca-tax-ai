import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "wayback_ecode": "http://archive.org/wayback/available?url=ecode360.com/44333256",
 "wayback_qcode": "http://archive.org/wayback/available?url=library.qcode.us/lib/malibu_ca/pub/municipal_code/item/title_5-chapter_5_55-5_55_130",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code)
        print(r.text[:1000])
    except Exception as e:
        print(name, "ERROR", e)
