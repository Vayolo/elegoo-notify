# Esempi di payload SDCP per test offline

Riferimento: https://docs.opencentauri.cc/software/api/ — il simulatore locale
(`tests/simulator.py`) implementa esattamente questi formati.

## Heartbeat

```
→ "ping"
← "pong"
```

## Richiesta (client → stampante)

```json
{
  "Id": "9d1a2b5c-0000-4000-8000-abcdef123456",
  "Data": {
    "Cmd": 0,
    "Data": {},
    "RequestID": "6f8b9c0d-1111-4222-8333-444455556666",
    "MainboardID": "SIM0001CENTAURI",
    "TimeStamp": 1760000000,
    "From": 0
  },
  "Topic": "sdcp/request/SIM0001CENTAURI"
}
```

## Risposta (stampante → client)

```json
{
  "Id": "uuid",
  "Data": {
    "Cmd": 0,
    "Data": { "Ack": 0 },
    "RequestID": "6f8b9c0d-1111-4222-8333-444455556666",
    "MainboardID": "SIM0001CENTAURI",
    "TimeStamp": 1760000000
  },
  "Topic": "sdcp/response/SIM0001CENTAURI"
}
```

## Push di stato — `sdcp/status/{MainboardID}` (evento principale)

```json
{
  "Id": "uuid",
  "Status": {
    "CurrentStatus": [1],
    "PreviousStatus": 0,
    "TempOfNozzle": 209.6,
    "TempTargetNozzle": 210,
    "TempOfHotbed": 59.8,
    "TempTargetHotbed": 60,
    "TempOfBox": 31.2,
    "TempTargetBox": 0,
    "CurrenCoord": "150.5,75.2,45.8",
    "CurrentFanSpeed": { "ModelFan": 80, "ModeFan": 80, "AuxiliaryFan": 50, "BoxFan": 0 },
    "LightStatus": { "SecondLight": 1 },
    "ZOffset": 0.0,
    "PrintSpeed": 100,
    "PrintInfo": {
      "Status": 1,
      "CurrentLayer": 45,
      "TotalLayer": 120,
      "CurrentTicks": 13500,
      "TotalTicks": 36000,
      "Filename": "test_cube.gcode",
      "ErrorNumber": 0,
      "TaskId": "task-0001",
      "PrintSpeed": 100
    }
  },
  "MainboardID": "SIM0001CENTAURI",
  "TimeStamp": 1760000000,
  "Topic": "sdcp/status/SIM0001CENTAURI"
}
```

Nota: `CurrenCoord` è il refuso ufficiale del protocollo (manca la 't').

## Codici `PrintInfo.Status` ed eventi derivati

| Codice | Significato        | Evento elegoo-notify |
|--------|--------------------|----------------------|
| 0      | idle               | (fine job)           |
| 1      | printing           | print_started / print_progress |
| 5      | pausing            | print_paused         |
| 6      | paused             | print_paused         |
| 7      | stopping           | —                    |
| 8      | stopped            | print_failed (user o errore) |
| 9      | complete           | print_completed      |
| 10     | file checking      | (pre-stampa)         |

Percentuale = `CurrentTicks/TotalTicks*100`, tempo rimanente =
`TotalTicks-CurrentTicks` secondi (i tick SDCP sono secondi).

## Comandi principali (Cmd)

| Cmd   | Azione            | Data                          |
|-------|-------------------|-------------------------------|
| 0     | refresh stato     | `{}`                          |
| 1     | refresh attributi | `{}`                          |
| 128   | start print       | `{"Filename": "x.gcode", "StartLayer": 0}` |
| 129   | pausa             | `{}`                          |
| 130   | stop              | `{}`                          |
| 131   | resume             | `{}`                          |
| 258   | file list          | `{"Url": "/local/"}`          |
| 321   | dettagli task     | `{"Id": ["task-0001"]}`       |
| 386   | video on/off       | `{"Enable": 1}` → risponde `{"Ack":0,"VideoUrl":"http://IP:3031/video"}` |

Ack Cmd 128: `0=OK 1=occupata 2=file non trovato 3=MD5 4=I/O 5=risoluzione 6=formato 7=modello`.

## Errore — `sdcp/error/{MainboardID}`

```json
{
  "Id": "uuid",
  "Data": {
    "Data": { "ErrorCode": 1 },
    "MainboardID": "SIM0001CENTAURI",
    "TimeStamp": 1760000000
  },
  "Topic": "sdcp/error/SIM0001CENTAURI"
}
```

## Motivi errore (ErrorStatusReason, via Cmd 321)

0=OK, 1=temperatura, 3=filamento finito, 6=filamento inceppato, 7=livellamento,
13/14/23=homing X/Z/Y, 18=distacco stampa, 19=eccezione stampa, 20=movimento
anormale, 24=errore file, 33/34=sensore temperatura offline.

## Upload HTTP GCODE

```
POST http://{IP}:3030/uploadFile/upload   (multipart/form-data)
S-File-MD5: <md5 hex del file>
Check: 1
Offset: 0
Uuid: <uuid hex arbitrario>
TotalSize: <dimensione in byte>
File: <binario>
```

Successo: `{"code": "000000", "messages": null, "data": {}, "success": true}`

## Webcam

- Stream MJPEG: `GET http://{IP}:3031/video` → `multipart/x-mixed-replace; boundary=--foo`
  (attivabile anche via Cmd 386; sulla Centauri il limite è 1 stream concorrente:
  per questo elegoo-notify apre UNA sola connessione e la ridistribuisce)
- Su questa stampante NON esiste `http://IP/webcam.jpg` (404), ma il campo
  `webcam.mode: "snapshot"` resta supportato per altri firmware.
