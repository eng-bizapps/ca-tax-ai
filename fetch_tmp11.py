import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "banning_survey": "https://banningca.gov/DocumentCenter/View/7338/Attachment-E---Survey_-Commercial-Cannabis-Activity-and-Associated-Tax-Rates",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code, len(r.content), r.headers.get('Content-Type'))
        with open(f"tmp_{name}.pdf","wb") as f:
            f.write(r.content)
    except Exception as e:
        print(name, "ERROR", e)
