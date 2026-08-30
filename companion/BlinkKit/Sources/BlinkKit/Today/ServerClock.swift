import Foundation

/// The one clock. Everything in the companion that needs to know what time it
/// is, or which day a datetime belongs to, asks this and nothing else.
///
/// **`Date()` is not the answer to "what day is it".** `src/api/server.py`'s
/// details handler says it plainly: every datetime in the payload is stamped
/// from the server's `now`, which is naive UTC, and `today` is the USER'S
/// local calendar day resolved from their stored timezone. A device sitting in
/// a different zone than the account must not disagree with the web app, so
/// nothing here consults the device's calendar, locale day boundary, or system
/// clock to decide which blocks are today.
///
/// The device clock is used for exactly one thing, and it is honest about it:
/// the "as of 9:41" stamp, which records when THIS DEVICE received a response.
/// That is a local event, not a server-dated fact.
public struct ServerClock: Sendable, Equatable {
    /// The server's instant, carried as a `Date` whose absolute value is the
    /// naive UTC string read as UTC.
    public let now: Date
    /// The user's local calendar day, exactly as the server published it.
    public let today: String
    /// The zone the server used. UTC when the profile has none, which is what
    /// `src/core/localtime.py` falls back to.
    public let timeZone: TimeZone

    public init(now: Date, today: String, timezoneIdentifier: String?) {
        self.now = now
        self.today = today
        // An unknown or missing zone resolves to UTC rather than to the
        // device's, mirroring `resolve_zone` in src/core/localtime.py. A
        // guessed offset would be a fabricated fact about the user's day.
        self.timeZone = timezoneIdentifier.flatMap(TimeZone.init(identifier:)) ?? .gmt
    }

    public init(details: WorkspaceDetails) {
        self.init(now: details.now, today: details.today, timezoneIdentifier: details.timezone)
    }

    // MARK: Day arithmetic, in the server's zone

    private var calendar: Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        return cal
    }

    /// The `YYYY-MM-DD` this instant falls on, in the user's zone. The Swift
    /// counterpart of `local_date` in `src/core/localtime.py`.
    public func localDay(of instant: Date) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: instant)
        guard let y = c.year, let m = c.month, let d = c.day else { return "" }
        return String(format: "%04d-%02d-%02d", y, m, d)
    }

    /// Does this instant fall on the user's today?
    public func isToday(_ instant: Date) -> Bool {
        localDay(of: instant) == today
    }

    /// The hour of the day, 0 to 23, in the user's zone.
    public func localHour(of instant: Date) -> Int {
        calendar.dateComponents([.hour], from: instant).hour ?? 0
    }

    /// The user's current local hour.
    public var localHourNow: Int { localHour(of: now) }

    /// Minutes since local midnight, 0 to 1439, in the user's zone. The plan's
    /// vertical timeline maps a block to a position with this, so it lands on
    /// the same hour the clock time reads (`clockTime`) — one zone, one answer.
    public func localMinuteOfDay(of instant: Date) -> Int {
        let c = calendar.dateComponents([.hour, .minute], from: instant)
        return (c.hour ?? 0) * 60 + (c.minute ?? 0)
    }

    /// The user's current minute-of-day, for the now-line.
    public var localMinuteOfDayNow: Int { localMinuteOfDay(of: now) }

    /// The short weekday and day-of-month for a `YYYY-MM-DD` the server
    /// published, read in the user's zone so the plan's day headers agree with
    /// every clock time on the same screen. A string the server did not shape
    /// as a date returns empty rather than a guessed one.
    public func calendarDay(from day: String) -> (weekdayShort: String, dayNumber: Int) {
        let parser = DateFormatter()
        parser.calendar = Calendar(identifier: .gregorian)
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.timeZone = timeZone
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: day) else { return ("", 0) }
        let dayNumber = calendar.component(.day, from: date)
        let label = DateFormatter()
        label.timeZone = timeZone
        label.locale = .autoupdatingCurrent
        label.setLocalizedDateFormatFromTemplate("EEE")
        return (label.string(from: date).uppercased(), dayNumber)
    }

    // MARK: Formatting

    /// A clock time in the user's zone, e.g. "9:41 AM".
    public func clockTime(_ instant: Date) -> String {
        let f = DateFormatter()
        f.timeZone = timeZone
        f.locale = .autoupdatingCurrent
        f.setLocalizedDateFormatFromTemplate("jmm")
        return f.string(from: instant)
    }

    // MARK: Wire format

    /// Parses the API's naive-UTC ISO strings. The server emits three shapes:
    /// `2026-08-28T08:06:51` (`now`, `timespec="seconds"`),
    /// `2026-08-28T07:00:00` (block bounds), and
    /// `2026-08-28T08:06:45.552868Z` (`created_at`, which carries a Z and
    /// microseconds). All three are UTC, so all three read as UTC.
    public static func date(from raw: String) throws -> Date {
        if let parsed = naiveUTCFormatter.date(from: normalised(raw)) {
            return parsed
        }
        throw DecodingError.dataCorrupted(
            .init(codingPath: [], debugDescription: "Not a server datetime: \(raw)")
        )
    }

    /// The inverse, for the cache's own round trip. Seconds precision, which
    /// is what the payload's own `now` uses.
    public static func string(from date: Date) -> String {
        naiveUTCFormatter.string(from: date)
    }

    /// Strip a trailing Z and any fractional seconds. Both are noise on a
    /// value the server has already told us is UTC.
    private static func normalised(_ raw: String) -> String {
        var value = raw
        if value.hasSuffix("Z") { value.removeLast() }
        if let dot = value.firstIndex(of: ".") { value = String(value[value.startIndex..<dot]) }
        return value
    }

    private static let naiveUTCFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .gmt
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()
}
