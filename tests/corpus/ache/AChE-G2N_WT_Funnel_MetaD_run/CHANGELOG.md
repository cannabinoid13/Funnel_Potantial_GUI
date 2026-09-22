# AChE-G2N değişiklik kaydı

Kaynak: Maestro Metadynamics GUI'sinin *Write* çıktısı (`AChE-G2N_WT_funnel_MetaD_run.cms/.cfg/.msj/.sh`).
Kaynak CMS SHA-256: `76487d225429d22c93ebb73e87159d6a4e11568a89dc5c306e57c0669aaee189`.

## Kaynak pakette saptananlar

- `.pot` dosyası, manifest, validator, motor preflight'ı ve analiz betiği bulunmuyordu.
- MSJ yalnız iki atom grubunun mesafesini biaslayan eski GUI MetaD bloğuydu; funnel ve
  well-tempered hill azaltımı içermiyordu.
- Sabit hill `0.03 kcal/mol`, hill aralığı `0.09 ps` idi; CVSEQ aralığı trajektori
  aralığına bağlanmıştı. Karşılaştırmalı referans protokolle eşdeğer değildi.
- Kaynak çalıştırıcı dosya varlığı, POT derleme, manifest ve eski çıktı çakışması
  kontrollerini yapmıyordu.

## Uygulanan dönüşüm

- Kaynak 41986 atomlu CMS aynen korundu (bayt bayt kopya, SHA-256 kilitli).
- Maestro GUI `meta={...}` bloğu ve boş `analysis` aşaması kaldırıldı; üretim
  `meta = FILE` ve `AChE-G2N_WT_funnel_MetaD_run.pot` dosyasına bağlandı.
- `backend.force.term.ES.interval` override'ı kaldırıldı; CVSEQ aralığı POT içindeki
  `declare_output` ile 2 ps'ye alındı.
- Ligandın 34 ağır atomu (AID 8286–8319)
  tek bias hedefi olarak tanımlandı; hidrojenler dışlandı.
- Referans paketle aynı PBC-güvenli O/U/V frame grupları ve yerel eksen/origin
  katsayıları taşındı; AID'ler yeni CMS'te kalıntı+atom adı eşleşmesiyle yeniden çözüldü.
- Aynı funnel skalerleri, flat-bottom duvarlar ve tek `z` WT-MetaD biası kullanıldı;
  funnel ligandın COM'una göre kaydırılmadı veya yeniden yöneltilmedi.
- Well-tempered parametreleri: `h0 = 0.25`, `sigma_z = 0.50 Å`,
  hill/CVSEQ 2 ps, `gamma = 15`,
  `ktemp = 8.624466483 kcal/mol`.
- CFG; 200 ns, 310 K, NPT–MTK, 2/2/6 fs, 20 ps DTR (center = solute), 2 ps enerji/simbox,
  500 ps checkpoint ve 1000 ps CMS çıktısına getirildi.
- Geçersiz `maeff_output.write_last_step` eklenmedi.
- Asp74, Trp86, Tyr124, Trp286, Phe297, Tyr337, Phe338, Tyr341 aynı tanımlarla çevrimiçi ve
  DTR-sonrası analiz kapsamına alındı; üç gate ölçümü, Tyr337 chi1/chi2 ve frame sağlık
  ölçüleri eklendi.
- Zorunlu manifest, statik validator, `enhsamp.parseStr` preflight'ı ve güvenli çıktı
  çakışma koruması eklendi.

## Değiştirilmeyenler

- CMS koordinatları, kutusu, kuvvet alanı, su/iyon içeriği, ligand mikrodurumu ve
  protein protonasyonu;
- referanstaki funnel boyutları, açı, merkez çizgisinin protein-relative tanımı, duvar
  sabitleri (`k_rad = 29.0`, `k_z = 50.0`) ve WT-MetaD parametreleri;
- takip edilen kalıntıların kimlikleri ve analiz tanımları;
- 200 ns ve 310 K üretim hedefi.

