# PAR Display (MSFS)

Minimal PAR-style display that reads injected AI traffic from MSFS via SimConnect and renders a two‑panel PAR view.

## Features
- Direct SimConnect connection (single executable)
- Configurable runways via JSON
- Runway selector dropdown

## Requirements
- Windows (MSFS + SimConnect)
- Python 3.11+ for local development

## Configuration
Edit `par_config.json`:
- `active_airport`: ICAO to use at startup
- `active_runway`: key of the runway to use at startup
- `airports`: map of airports, each with a `runways` map
- `target_callsign`: set to a callsign to lock the display
- `poll_hz`: data polling rate (e.g. 2.0)
- `window_size`: [width, height]

## Build (Windows)
This repo includes a GitHub Actions workflow to build a standalone Windows exe.
Run the workflow in GitHub Actions and download the artifact.

## Local run
```bash
python par_app.py
```
