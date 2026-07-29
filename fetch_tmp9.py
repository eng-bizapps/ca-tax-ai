import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
urls = {
 "perris_measurej": "https://ballotpedia.org/Perris,_California,_Medical_Marijuana_Dispensary_and_Cultivation_Tax,_Measure_J_(November_2016)",
 "perris_measureg": "https://ballotpedia.org/Perris,_California,_Measure_G,_Marijuana_Tax_(November_2018)",
 "palmsprings_ord1933": "https://www.palmspringsca.gov/home/showdocument?id=53848",
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
