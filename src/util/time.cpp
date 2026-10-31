// Copyright (c) 2009-2010 Satoshi Nakamoto
// Copyright (c) 2009-2022 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#if defined(HAVE_CONFIG_H)
#include <config/syscoin-config.h>
#endif

#include <compat/compat.h>
#include <tinyformat.h>
#include <util/time.h>
#include <util/check.h>

#include <atomic>
#include <chrono>
#include <ctime>
#include <locale>
#include <thread>
#include <sstream>
#include <string>

void UninterruptibleSleep(const std::chrono::microseconds& n) { std::this_thread::sleep_for(n); }

static std::atomic<int64_t> nMockTime(0); //!< For testing

bool ChronoSanityCheck()
{
    // std::chrono::system_clock.time_since_epoch and time_t(0) are not guaranteed
    // to use the Unix epoch timestamp, prior to C++20, but in practice they almost
    // certainly will. Any differing behavior will be assumed to be an error, unless
    // certain platforms prove to consistently deviate, at which point we'll cope
    // with it by adding offsets.

    // Create a new clock from time_t(0) and make sure that it represents 0
    // seconds from the system_clock's time_since_epoch. Then convert that back
    // to a time_t and verify that it's the same as before.
    const time_t time_t_epoch{};
    auto clock = std::chrono::system_clock::from_time_t(time_t_epoch);
    if (std::chrono::duration_cast<std::chrono::seconds>(clock.time_since_epoch()).count() != 0) {
        return false;
    }

    time_t time_val = std::chrono::system_clock::to_time_t(clock);
    if (time_val != time_t_epoch) {
        return false;
    }

    // Check that the above zero time is actually equal to the known unix timestamp.
    struct tm epoch;
#ifdef HAVE_GMTIME_R
    if (gmtime_r(&time_val, &epoch) == nullptr) {
#else
    if (gmtime_s(&epoch, &time_val) != 0) {
#endif
        return false;
    }

    if ((epoch.tm_sec != 0)  ||
       (epoch.tm_min  != 0)  ||
       (epoch.tm_hour != 0)  ||
       (epoch.tm_mday != 1)  ||
       (epoch.tm_mon  != 0)  ||
       (epoch.tm_year != 70)) {
        return false;
    }
    return true;
}

NodeClock::time_point NodeClock::now() noexcept
{
    const std::chrono::seconds mocktime{nMockTime.load(std::memory_order_relaxed)};
    const auto ret{
        mocktime.count() ?
            mocktime :
            std::chrono::system_clock::now().time_since_epoch()};
    assert(ret > 0s);
    return time_point{ret};
};

void SetMockTime(int64_t nMockTimeIn)
{
    Assert(nMockTimeIn >= 0);
    nMockTime.store(nMockTimeIn, std::memory_order_relaxed);
}

void SetMockTime(std::chrono::seconds mock_time_in)
{
    nMockTime.store(mock_time_in.count(), std::memory_order_relaxed);
}

std::chrono::seconds GetMockTime()
{
    return std::chrono::seconds(nMockTime.load(std::memory_order_relaxed));
}

int64_t GetTime() { return GetTime<std::chrono::seconds>().count(); }

namespace {
// Broken-down UTC date/time, computed without the C library so the result
// does not depend on the platform's gmtime range. (MSVC's gmtime_s rejects
// every time after 3000-12-31, which made FormatISO8601* return an empty
// string on Windows for inputs that POSIX gmtime_r formats fine, e.g. the
// year-32767 values exercised by the unit tests.)
struct BrokenDownUTC {
    int64_t year;   //!< Proleptic Gregorian year (astronomical numbering)
    int month;      //!< [1, 12]
    int day;        //!< [1, 31]
    int hour;       //!< [0, 23]
    int minute;     //!< [0, 59]
    int second;     //!< [0, 59]
};

// Convert seconds since the Unix epoch to a broken-down UTC date/time.
// Days-to-civil conversion after Howard Hinnant's public-domain algorithm
// (https://howardhinnant.github.io/date_algorithms.html); exact for the
// whole int64_t input range and identical to POSIX gmtime semantics
// (proleptic Gregorian calendar, no leap seconds).
BrokenDownUTC BreakDownUTC(int64_t nTime)
{
    // Split into whole days and a non-negative second-of-day, using floor
    // division so negative (pre-epoch) times are handled correctly.
    int64_t days = nTime / 86400;
    int64_t secs = nTime % 86400;
    if (secs < 0) {
        secs += 86400;
        --days;
    }

    BrokenDownUTC out;
    out.hour = int(secs / 3600);
    out.minute = int((secs % 3600) / 60);
    out.second = int(secs % 60);

    const int64_t z = days + 719468; // days since 0000-03-01
    const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const uint64_t doe = uint64_t(z - era * 146097);                    // [0, 146096]
    const uint64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    const int64_t y = int64_t(yoe) + era * 400;
    const uint64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);       // [0, 365]
    const uint64_t mp = (5 * doy + 2) / 153;                            // [0, 11]
    out.day = int(doy - (153 * mp + 2) / 5 + 1);                        // [1, 31]
    out.month = int(mp < 10 ? mp + 3 : mp - 9);                         // [1, 12]
    out.year = y + (out.month <= 2);
    return out;
}
} // namespace

std::string FormatISO8601DateTime(int64_t nTime) {
    const BrokenDownUTC ts = BreakDownUTC(nTime);
    return strprintf("%04i-%02i-%02iT%02i:%02i:%02iZ", ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second);
}

std::string FormatISO8601Date(int64_t nTime) {
    const BrokenDownUTC ts = BreakDownUTC(nTime);
    return strprintf("%04i-%02i-%02i", ts.year, ts.month, ts.day);
}
// SYSCOIN
std::string DurationToDHMS(int64_t nDurationTime)
{
	int seconds = nDurationTime % 60;
	nDurationTime /= 60;
	int minutes = nDurationTime % 60;
	nDurationTime /= 60;
	int hours = nDurationTime % 24;
	int days = nDurationTime / 24;
	if (days)
		return strprintf("%dd %02dh:%02dm:%02ds", days, hours, minutes, seconds);
	if (hours)
		return strprintf("%02dh:%02dm:%02ds", hours, minutes, seconds);
	return strprintf("%02dm:%02ds", minutes, seconds);
}

struct timeval MillisToTimeval(int64_t nTimeout)
{
    struct timeval timeout;
    timeout.tv_sec  = nTimeout / 1000;
    timeout.tv_usec = (nTimeout % 1000) * 1000;
    return timeout;
}

struct timeval MillisToTimeval(std::chrono::milliseconds ms)
{
    return MillisToTimeval(count_milliseconds(ms));
}
