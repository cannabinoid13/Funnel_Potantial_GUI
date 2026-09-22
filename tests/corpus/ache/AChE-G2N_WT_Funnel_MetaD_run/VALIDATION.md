# AChE-G2N paket doğrulama kaydı

Doğrulama tarihi: 2026-08-31

## Paket ve sistem kimliği

- Kaynak CMS SHA-256: `76487d225429d22c93ebb73e87159d6a4e11568a89dc5c306e57c0669aaee189`.
- Tam sistem: 41986 atom.
- Ortorombik kutu: `76.781247 × 84.400749 × 68.503793 Å`.
- Ligand: zincir B, LIG1 rezidü 1; 34 ağır atom.
- Bias ligand ağır AID'leri: 8286–8319.
- Fiziksel geometri kilidi: `20bdb179f05f83dd3dfbc0a253fa0b0d6d8b6079a0bc715f9520011249fc7f60`.

## Başlangıç sayısal kontrolleri

- eksen: `(0.3997277548580135, -0.9089113780663901, -0.11873427819137787)`;
- origin: `(1.6612985960336033, -10.245141943749921, -5.061455841391394) Å`;
- ligand `z = 1.4353079460726115 Å`;
- ligand `rho = 1.8267959316755318 Å`;
- izinli başlangıç yarıçapı `13.111615555867706 Å`;
- frame `OU/OV/UV = 11.154314/18.371845/18.651493 Å`;
- Gram–Schmidt yüksekliği `17.646002 Å`;
- frame condition sine `0.960492` (> 0.90 gerekli);
- frame anchor cosine `0.278309` (|·| < 0.35 gerekli);
- en kısa yarım kutu `34.251897 Å`.

Validator 36 sabit dönüşe ek olarak 2048 Haar-dağılımlı rastgele dönüş,
kutunun dört katına kadar öteleme ve atomların bağımsız PBC yeniden görüntülenmesini
uygular. Eksen, origin, ligand `z/rho` ve izinli funnel yarıçapı toplam 2084 sınamada invariant kalmalıdır — **geçti**.

## Well-tempered protokol kontrolleri

- 200 ns, 310 K, NPT–MTK, 1.01325 bar ve 2/2/6 fs RESPA.
- Tek bias CV: ligandın 34 ağır atomlu COM `z` koordinatı (`declare_meta.dimension = 1`).
- `rho` yalnız flat-bottom funnel restraint'idir, biaslanmaz.
- Well-tempered kurgu POT içinde iki `meta(0,...)` çağrısıyla kurulur: sıfır yükseklikli
  sonda + `hill = h0*exp(v_old/(-ktemp))`.
- `h0 = 0.25 kcal/mol`, `sigma_z = 0.50 Å`, hill/CVSEQ aralığı 2 ps, `gamma = 15`, `ktemp = 8.624466483 kcal/mol`.
- Trajektori 20 ps, enerji/simbox 2 ps, checkpoint 500 ps.
- Geçersiz `maeff_output.write_last_step` ve eski GUI `meta={...}` bloğu yoktur;
  `analysis { meta = {} }` aşaması kaldırılmıştır.
- Manifest, çıktı çakışma koruması, statik validator ve zorunlu motor preflight'ı bulunur.

## Kurulum-bağımlı kontrol

Bu paketi hazırlayan ortamda lisanslı Schrödinger motoru bulunmadığından gerçek MD
başlatılmamıştır. Çalıştırma betiği kullanıcının kurulumunda `topo.read_cms` ile bütün
AID'leri çözer ve POT'u `enhsamp.parseStr` ile derler. Bu adım atlanamaz; başarısız
olursa `multisim` çağrılmaz.

## Bilimsel karşılaştırılabilirlik uyarısı

- `A:396` bu CMS'te **GLU**, referans pakette **GLH**.

Bu fark POT/funnel hatası değildir; verilen CMS değiştirilmemiştir.

