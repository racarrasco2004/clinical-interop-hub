# Clinical Interop Hub

Megaproyecto de **interoperabilidad clínica** en Python. Une tres piezas que en un hospital real viven en sistemas distintos:

| Pieza | Qué hace |
|---|---|
| **Servidor FHIR** | Guarda pacientes, atenciones, resultados, diagnósticos y estudios de imagen en el formato estándar HL7 FHIR R4 |
| **Conversor HL7 v2 → FHIR** | Traduce mensajes antiguos (ADT, ORU) al formato FHIR |
| **Visor DICOM web** | Muestra imágenes médicas (TC, radiografía, RM) en el navegador y las vincula al paciente FHIR |

Todo usa **datos ficticios**. Es un proyecto educativo: no es un dispositivo médico ni sirve para diagnóstico.

```
Sistema de laboratorio ──HL7 v2──► Conversor ──┐
                                                ├──► API FHIR (SQLite) ◄── Visor DICOM (navegador)
Equipo de imagen (TC, Rx) ──DICOM──► /dicom/upload ┘
```

La idea clave: **un mismo paciente** aparece en las tres piezas. El mensaje HL7 crea a "Ana Pérez" (ficha 12345678); cuando subes una imagen DICOM con ese mismo ID, queda asociada a ella mediante un recurso `ImagingStudy`.

---

## Guía paso a paso (desde cero)

### 1. Instala lo necesario (una sola vez)

1. **Python 3.10 o superior**: https://www.python.org/downloads/
   En Windows, marca la casilla **"Add python.exe to PATH"** al instalar.
2. **Visual Studio Code**: https://code.visualstudio.com/ (el editor donde abrirás el proyecto).
3. Opcional, para subirlo a GitHub: **Git** (https://git-scm.com/) y una cuenta en https://github.com

Comprueba que Python quedó bien: abre una terminal y escribe `python --version` (en Mac/Linux puede ser `python3 --version`).

### 2. Abre el proyecto

1. Descomprime el ZIP en una carpeta, por ejemplo `Documentos/clinical-interop-hub`.
2. En VS Code: **Archivo → Abrir carpeta…** y elige `clinical-interop-hub`.
3. Abre la terminal integrada: **Terminal → Nueva terminal**.

### 3. Crea el entorno e instala las dependencias

```bash
python -m venv .venv
```

Actívalo:

- Windows (PowerShell): `.venv\Scripts\Activate.ps1`
  (si da error de permisos: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` y vuelve a intentar)
- Windows (CMD): `.venv\Scripts\activate.bat`
- Mac / Linux: `source .venv/bin/activate`

Luego:

```bash
pip install -r requirements.txt
```

> El entorno virtual (`.venv`) es una "caja" aislada para las librerías del proyecto. Así no ensucias el Python de tu computador.

### 4. Ejecuta el servidor

```bash
uvicorn app.main:app --reload
```

Déjalo corriendo. Abre en el navegador:

- **http://127.0.0.1:8000** → el visor DICOM
- **http://127.0.0.1:8000/docs** → documentación interactiva de la API (Swagger), donde puedes probar todo con botones

Para detenerlo: `Ctrl + C` en la terminal.

### 5. Prueba el flujo completo (3 minutos)

Abre una **segunda terminal** (con el entorno activado) y ejecuta:

```bash
# a) Un mensaje HL7 v2 se convierte a FHIR y se guarda: crea paciente, atención y 3 resultados
python -m app.converter samples/hl7/oru_r01.hl7 --post http://127.0.0.1:8000
```

Luego, en el visor (http://127.0.0.1:8000):

```
b) Presiona "Cargar imágenes de ejemplo"  → se suben una TC y una radiografía ficticias
c) Mira el panel derecho "FHIR": 1 Patient, 1 Encounter, 3 Observation, 2 ImagingStudy
```

Eso demuestra la integración: **el DICOM se asoció al mismo paciente que creó el HL7**.

### 6. Corre las pruebas automáticas

```bash
pytest -v
```

---

## Dashboard clínico

1. Con el servidor corriendo, abre http://127.0.0.1:8000/dashboard
2. Pulsa **Generar datos de demostración** (tarda unos segundos). Crea 3.000 pacientes ficticios con atenciones, diagnósticos CIE-10, signos vitales e imágenes.
3. Cambia el período (7/30/90 días) y las camas totales; todo se recalcula.

Indicadores: atenciones, ocupación de camas, estadía promedio, espera en urgencias (mediana y P90), reingreso a 30 días y alertas críticas (signos vitales HH/LL). Cada gráfico tiene botón **Ver tabla**.

Los datos se calculan en vivo desde los recursos FHIR guardados, así que lo que subas por HL7 o DICOM también cuenta. Los datos de demostración llevan una etiqueta (`meta.tag`) y el botón *Borrar demo* solo borra esos.

Por consola: `python -m app.seed_demo`

---

## Cómo se usa el visor

| Acción | Cómo |
|---|---|
| Cambiar de corte | Rueda del mouse, flechas ↑ ↓, o la barra inferior |
| Brillo y contraste (ventana/nivel) | Herramienta **Brillo/Contraste** y arrastrar |
| Ventanas de TC | Botones Partes blandas, Pulmón, Hueso, Cerebro (solo para TC) |
| Zoom | Herramienta **Zoom**, o `Ctrl` + rueda |
| Mover la imagen | Herramienta **Mover** |
| Medir distancias | Herramienta **Regla** (en mm/cm usando el tamaño real del pixel) |
| Invertir / reiniciar | Teclas `I` y `R` |
| Ver las etiquetas DICOM | Pestaña **Etiquetas DICOM** (con filtro) |
| Subir tus archivos | Arrastra archivos `.dcm` al recuadro de la izquierda |

---

## Conceptos clave (simple)

**DICOM** es el formato de las imágenes médicas. Un archivo `.dcm` trae dos cosas: la imagen (píxeles) y muchas **etiquetas** con datos (paciente, equipo, fecha, tamaño del pixel...). Se organiza así:

```
Paciente → Estudio (ej. TC de tórax) → Serie (ej. cortes axiales) → Imagen (un corte)
```

**Unidades Hounsfield (HU)**: en una TC cada pixel es un número que indica qué tan denso es el tejido (aire ≈ -1000, agua = 0, hueso > 300). El archivo guarda un número "crudo" y el visor aplica `RescaleSlope` y `RescaleIntercept` para obtener HU.

**Ventana y nivel (window / level)**: la pantalla muestra solo 256 grises, pero la TC tiene miles de valores. La *ventana* elige qué rango mostrar: **nivel** = centro del rango, **ancho** = cuánto abarca. Con ventana de pulmón (-600 / 1500) ves el aire; con la de hueso (500 / 2000) ves los huesos.

**ImagingStudy (FHIR)**: el recurso que representa un estudio de imagen. Guarda a qué paciente pertenece y la lista de series e imágenes (por sus UID), pero **no** los píxeles. Los píxeles quedan en archivos `.dcm` en `dicom_files/`.

---

## Mapa del proyecto

```
clinical-interop-hub/
├── app/
│   ├── main.py            API FHIR + endpoints DICOM (FastAPI y SQLite)
│   ├── converter.py       Conversor HL7 v2 → FHIR
│   ├── dicom_service.py   Lee DICOM, arma Patient/ImagingStudy, prepara la imagen
│   ├── sample_dicom.py    Genera las imágenes DICOM ficticias
│   ├── dashboard.py       Calcula los indicadores del dashboard
│   ├── seed_demo.py       Genera un hospital ficticio (≈4 meses de datos)
│   ├── static/dashboard.html Dashboard clínico (SVG, claro/oscuro)
│   └── static/viewer.html Visor web (HTML + JavaScript, sin librerías externas)
├── samples/
│   ├── hl7/               Mensajes HL7 v2 de ejemplo (ADT y ORU)
│   └── dicom/             TC y radiografía ficticias (.dcm)
├── tests/                 65 pruebas automáticas
├── requirements.txt
└── README.md
```

### Endpoints principales

| Método y ruta | Qué hace |
|---|---|
| `GET /` · `GET /viewer` | Visor DICOM |
| `POST /convert/hl7v2?store=true` | Convierte un mensaje HL7 v2 a FHIR y (opcional) lo guarda |
| `POST /dicom/upload` | Recibe un `.dcm`, crea o reutiliza el Patient y el ImagingStudy |
| `POST /dicom/samples` | Carga las imágenes de ejemplo |
| `GET /dicom/studies` | Estudios, series y paciente (lo usa el visor) |
| `GET /dicom/instances/{uid}/frames/{n}` | Un corte como enteros de 16 bits |
| `GET /dicom/instances/{uid}/tags` | Etiquetas DICOM legibles |
| `GET/POST/PUT/DELETE /{Patient\|Encounter\|Observation\|Condition\|ImagingStudy}` | API FHIR |
| `GET /Patient/{id}/$everything` | Todo lo registrado de un paciente |
| `POST /` | Transacción FHIR (Bundle): guarda todo o nada |
| `GET /metadata` | CapabilityStatement |
| `GET /dashboard` | Dashboard clínico |
| `GET /dashboard/data?days=30&capacity=60` | Indicadores en JSON |
| `POST /dashboard/seed` · `DELETE /dashboard/seed` | Genera / borra datos de demostración (solo los ficticios) |

---

## Cómo funciona (para explicarlo en una entrevista)

1. **HL7 → FHIR** (`converter.py`): lee el mensaje por segmentos (`PID`→Patient, `PV1`→Encounter, `OBX`→Observation, `DG1`→Condition), convierte fechas, sexo y códigos (LOINC, CIE-10), y arma un Bundle de tipo *transaction*. La API lo guarda de forma atómica y resuelve las referencias `urn:uuid`.
2. **Mismo paciente** (`find_patient_by_identifier`): antes de crear un Patient, la API busca uno con el mismo identificador. Por eso HL7 y DICOM terminan en la misma persona. Si el DICOM trae `IssuerOfPatientID` (ej. `HOSPITAL_DEMO`), se usa el mismo `system` que el conversor.
3. **DICOM → FHIR** (`dicom_service.py`): `pydicom` lee las etiquetas y arma un `ImagingStudy` con sus series e imágenes (idempotente: subir la misma imagen dos veces no la duplica).
4. **Visor**: el servidor entrega cada corte como un arreglo de enteros de 16 bits ya en HU; el navegador aplica ventana/nivel con una tabla de 256 grises y lo dibuja en un `<canvas>`. La regla usa `PixelSpacing` para dar milímetros reales.
5. **Integridad**: no se puede crear un estudio sin paciente, ni borrar un paciente que tenga registros; al borrar un estudio se borran sus archivos.

---

## Usar tus propios DICOM

- **Importante: usa solo imágenes anonimizadas o de ejemplo públicas.** Los DICOM reales traen nombre, ID y fechas del paciente. Nunca subas datos de pacientes reales a un proyecto de portafolio.
- Para práctica hay bibliotecas públicas de ejemplo (por ejemplo la de OsiriX) y `pydicom` trae archivos de prueba: `python -c "from pydicom.data import get_testdata_file; print(get_testdata_file('CT_small.dcm'))"` muestra la ruta de uno.
- El DICOM debe traer `PatientID`, `StudyInstanceUID`, `SeriesInstanceUID` y `SOPInstanceUID`.

## Limitaciones conocidas

- Visor de **escala de grises** (las imágenes a color se convierten a gris). Sin 3D, sin reformateo multiplanar.
- Formatos comprimidos: funcionan los más comunes (se probó JPEG 2000); otros pueden requerir `pip install pylibjpeg pylibjpeg-libjpeg`.
- Validación FHIR básica propia (no valida contra los perfiles oficiales) y sin autenticación.
- Los valores se guardan como enteros de 16 bits con signo; las imágenes con valores fuera de ese rango se recortan.
- Pensado para uso local y datos de prueba, no para producción.

## Ideas para seguir

- Autenticación (OAuth2 / SMART on FHIR) y roles
- Más recursos (DiagnosticReport con informe radiológico) y mensajes HL7 (ORM, SIU)
- Docker y despliegue
- Panel de indicadores con los datos del hub (siguiente proyecto)

## Tecnologías

Python · FastAPI · SQLite · pydicom · NumPy · HL7 v2 · HL7 FHIR R4 · DICOM · LOINC · CIE-10 · JavaScript (Canvas) · pytest
