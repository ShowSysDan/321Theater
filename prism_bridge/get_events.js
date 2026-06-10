'use strict';
/**
 * Bridge script: fetch events from the Prism FM API.
 * Validated in the PrismSDKTest project; kept byte-compatible in output shape.
 *
 * Usage: node get_events.js '<json_args>'
 *
 * JSON args (all optional):
 *   startDate    - YYYY-MM-DD
 *   endDate      - YYYY-MM-DD
 *   eventStatus  - array of EventStatus values (0=HOLD,2=CONFIRMED,3=IN_SETTLEMENT,4=SETTLED)
 *   lastUpdated  - YYYY-MM-DD
 *   showType     - 'all' | 'rental' | 'talent'
 *   includeArchivedEvents - boolean
 *
 * Outputs a JSON array of event summary objects on stdout.
 * Errors go to stderr as a JSON object; exit code 1 on failure.
 * Requires the PRISM_TOKEN environment variable (set by prism_module.py).
 */

const { getPrism, fail, parseArgs } = require('./_sdk_loader');

const args = parseArgs();

function summarizeEvent(event) {
  return {
    id: event.id,
    name: event.name,
    event_status: event.eventStatus,
    event_status_string: event.eventStatusString,
    first_date: event.firstDate,
    last_date: event.lastDate,
    date_range_string: event.dateRangeString,
    venue_id: event.venueId,
    venue_name: event.venueName,
    venue_address: event.venueAddress,
    venue_city: event.venueCity,
    venue_state: event.venueState,
    stage_names: event.stageNames,
    is_archived: event.isArchived,
    is_rental: event.isForRentalEvent,
    tour_name: event.tourName,
    number_of_shows: event.numberOfShows,
    capacity: event.capacity,
    event_last_updated: event.eventLastUpdated,
    event_created_date: event.eventCreatedDate,
    age_limit: event.ageLimit,
    ticketing_url: event.ticketingURL,
    // Per-date schedule: [{date, allDay, startTime, endTime, stageName}, ...]
    dates: Array.isArray(event.dates) ? event.dates : [],
  };
}

async function main() {
  const prism = getPrism();
  const events = await prism.getEvents(args, {
    // SDK progress chatter goes to stderr so stdout stays clean JSON.
    onProgress: (p) => process.stderr.write(p.toString() + '\n'),
  });
  process.stdout.write(JSON.stringify(events.map(summarizeEvent)));
}

main().catch(fail);
