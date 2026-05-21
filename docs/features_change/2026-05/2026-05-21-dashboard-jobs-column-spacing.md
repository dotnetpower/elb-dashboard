# Dashboard jobs column spacing

## Motivation

The dashboard Jobs preview could place the Status pill too close to the Time column when job titles and metadata chips consumed most of the row width.

## User-facing change

The Jobs preview reserves a little more horizontal space for Status and Time, which trims the Job column slightly and prevents those right-side columns from crowding each other.

## API/IaC diff summary

- Frontend: adjusted the Cluster Pulse job row and header grid template used by the dashboard Jobs list.
- API: no changes.
- IaC: no changes.

## Validation evidence

- `cd web && npm run build` passed.
- Browser DOM layout check on the dashboard Jobs list showed the first row using `Status` width 92 px, `Time` width 104 px, and a 10 px gap between them, with no overlap.