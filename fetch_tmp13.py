import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "perris_doc2831": "https://www.cityofperris.org/home/showpublisheddocument/2831/637250455887970000",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code, len(r.content), r.headers.get('Content-Type'))
        ct = r.headers.get('Content-Type','')
        if 'pdf' in ct.lower():
            with open(f"tmp_{name}.pdf","wb") as f:
                f.write(r.content)
        else:
            with open(f"tmp_{name}.html","w",encoding="utf-8") as f:
                f.write(r.text)
    except Exception as e:
        print(name, "ERROR", e)
