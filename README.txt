ENERGIEVAKMAN UNIEC3 API - SNELSTART

LOKAAL TESTEN OP MAC
1. Zet deze map op je Mac.
2. Open Terminal.
3. Ga naar de map, bijvoorbeeld:
   cd ~/Downloads/energievakman-uniec-api
4. Installeer onderdelen:
   pip3 install -r requirements.txt
5. Start de API:
   uvicorn app:app --reload
6. Open in je browser:
   http://127.0.0.1:8000/docs
7. Klik op POST /generate > Try it out.
8. Vul in:
   {"address":"Vlierboomstraat 652, Den Haag"}
9. Klik Execute. Je krijgt een .uniec3 bestand terug.

ONLINE ZETTEN OP RAILWAY/RENDER
Start command:
uvicorn app:app --host 0.0.0.0 --port $PORT

SOFTR/MAKE
Stuur een POST request naar:
https://jouw-server-url/generate

Body JSON:
{"address":"Vlierboomstraat 652, Den Haag"}

Optioneel met handmatige hoogte:
{"address":"Vlierboomstraat 652, Den Haag", "height":10.2}
