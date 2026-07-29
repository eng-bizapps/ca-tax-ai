import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "perris_5.54": "https://library.municode.com/ca/perris/codes/code_of_ordinances?nodeId=COOR_TIT5BURELI_CH5.54MEMADIREPR",
}
for name,url in urls.items():
    try:
        r = requests.get(url, headers=H, timeout=20)
        print(name, r.status_code, len(r.text))
        with open(f"tmp_{name}.html","w",encoding="utf-8") as f:
            f.write(r.text)
    except Exception as e:
        print(name, "ERROR", e)
