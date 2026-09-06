import EventKit
import Foundation

struct CalendarEvent: Codable {
    let title: String
    let calendar: String
    let start: Date
    let end: Date
    let notes: String?
    let isAllDay: Bool
    let attendees: [String]
}

enum CalendarQueryError: Error, CustomStringConvertible {
    case accessDenied
    case accessRestricted
    case accessWriteOnly

    var description: String {
        switch self {
        case .accessDenied:
            return "Calendar access was denied."
        case .accessRestricted:
            return "Calendar access is restricted."
        case .accessWriteOnly:
            return "Calendar access is write-only; full access is required to read events."
        }
    }
}

@main
struct CalendarQuery {
    static func main() async {
        do {
            let store = EKEventStore()

            try await ensureAccess(to: store)

            let now = Date()
            let calendar = Calendar.current

            let requestedDays: Int
            if CommandLine.arguments.count > 1,
               let parsed = Int(CommandLine.arguments[1]),
               parsed > 0 {
                requestedDays = parsed
            } else {
                requestedDays = 1
            }

            let queryStart = calendar.startOfDay(for: now)
            let queryEnd = calendar.date(
                byAdding: .day,
                value: requestedDays,
                to: queryStart
            )!

            let predicate = store.predicateForEvents(
                withStart: queryStart,
                end: queryEnd,
                calendars: nil
            )

            let events = store.events(matching: predicate)
                .filter {
                    !$0.isAllDay
                }
                .sorted {
                    $0.startDate < $1.startDate
                }
                .map {
                    CalendarEvent(
                        title: $0.title ?? "",
                        calendar: $0.calendar.title,
                        start: $0.startDate,
                        end: $0.endDate,
                        notes: $0.notes,
                        isAllDay: $0.isAllDay,
                        attendees: ($0.attendees ?? []).compactMap { attendee -> String? in
                            let url = attendee.url
                            let absolute = url.absoluteString

                            if url.scheme?.lowercased() == "mailto" {
                                let address = String(
                                    absolute.dropFirst("mailto:".count)
                                )
                                return address.removingPercentEncoding ?? address
                            }

                            return absolute
                        }
                    )
                }

            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            encoder.dateEncodingStrategy = .iso8601

            let data = try encoder.encode(events)

            guard let output = String(data: data, encoding: .utf8) else {
                fatalError("Unable to encode JSON")
            }

            print(output)

        } catch {
            fputs("calendar-query: \(error)\n", stderr)
            exit(1)
        }
    }

    static func ensureAccess(to store: EKEventStore) async throws {
        let status = EKEventStore.authorizationStatus(for: .event)

        switch status {
        case .fullAccess:
            return

        case .notDetermined:
            let granted = try await store.requestFullAccessToEvents()

            if !granted {
                throw CalendarQueryError.accessDenied
            }

        case .denied:
            throw CalendarQueryError.accessDenied

        case .restricted:
            throw CalendarQueryError.accessRestricted

        case .writeOnly:
            throw CalendarQueryError.accessWriteOnly

        case .authorized:
            // Legacy authorization state.
            return

        @unknown default:
            throw CalendarQueryError.accessDenied
        }
    }
}
