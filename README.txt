Render stappen:
1. Upload deze bestanden naar een nieuwe GitHub repository.
2. Render > New > Web Service.
3. Koppel GitHub en kies deze repository.
4. Build Command: pip install -r requirements.txt
5. Start Command: gunicorn app:app
6. Deploy.
7. Test: https://JOUW-RENDER-URL.onrender.com/
8. Test download: https://JOUW-RENDER-URL.onrender.com/generate?address=Vlierboomstraat%20652,%20Den%20Haag

Softr/Make kan /generate aanroepen met GET of POST.
POST body voorbeeld:
{"address":"Vlierboomstraat 652, Den Haag"}

Optionele fallbacks:
height, bouwjaar, pand_id, gebruiksoppervlakte/go
