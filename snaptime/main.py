

import re
from functools import reduce
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, time
from dateutil.relativedelta import relativedelta


class SnapParseError(Exception):
    pass


class SnapUnitError(Exception):
    pass


SECONDS = "seconds"
MINUTES = "minutes"
HOURS = "hours"
DAYS = "days"
WEEKS = "weeks"
MONTHS = "months"
YEARS = "years"

# see also
# http://docs.splunk.com/Documentation/Splunk/latest/SearchReference/SearchTimeModifiers#How_to_specify_relative_time_modifiers
UNIT_LISTS = {
    SECONDS: ["s", "sec", "secs", "second", "seconds"],
    MINUTES: ["m", "min", "minute", "minutes"],
    HOURS: ["h", "hr", "hrs", "hour", "hours"],
    DAYS: ["d", "day", "days"],
    WEEKS: ["w", "week", "weeks"],
    MONTHS: ["mon", "month", "months"],
    # "quarters": ["q", "qtr", "qtrs", "quarter", "quarters"],  # not supported by relativedelta
    YEARS: ["y", "yr", "yrs", "year", "years"]
}


def get_unit(string):

    for unit, variants in UNIT_LISTS.items():
        if string in variants:
            return unit

    raise SnapUnitError("Unknown unit string '%s'" % string)


def get_weekday(string):
    result = get_num(string, default=None)
    if result and not (result >= 0 and result <= 7):
        raise SnapParseError("Bad weekday '%s'" % str(result))
    return result


def get_num(string, default=1):
    if string is None or string == "":
        return default

    return int(string)


def get_mult(string):
    return -1 if string == "-" else 1


def truncate(datetime_, unit):
    if unit == "seconds":
        result = datetime_.replace(microsecond=0)
    elif unit == "minutes":
        result = datetime_.replace(second=0, microsecond=0)
    elif unit == "hours":
        result = datetime_.replace(minute=0, second=0, microsecond=0)
    elif unit == "days":
        result = datetime_.replace(hour=0, minute=0, second=0, microsecond=0)
    elif unit == "months":
        result = datetime_.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif unit == "years":
        result = datetime_.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    return result


D_GENERAL = r"[-+]?\d+[a-zA-Z]+"
D_DETAILS = r"(?P<sign>[-+]?)(?P<num>\d+)(?P<unit_string>[a-zA-Z]+)"
D_PATTERN = re.compile(D_DETAILS)

S_GENERAL = r"@[a-zA-Z]+\d*"
S_DETAILS = r"@(?P<unit_string>[a-zA-Z]+)(?P<weekday>\d*)"
S_PATTERN = re.compile(S_DETAILS)

HEAD_PATTERN = re.compile(r"^({snap}|{delta})(.*)".format(snap=S_GENERAL, delta=D_GENERAL))


class SnapTransformation(object):
    def __init__(self, group):
        matchdict = S_PATTERN.match(group).groupdict()
        assert matchdict
        self.unit = get_unit(matchdict.get("unit_string"))
        self.weekday = get_weekday(matchdict.get("weekday"))

    def apply_to(self, dttm):
        result = dttm

        if self.unit == "weeks" and self.weekday:
            result = result - timedelta((result.isoweekday() - self.weekday) % 7)
            result = truncate(result, "days")
        elif self.unit == "weeks":
            result = result - timedelta((dttm.isoweekday() - 0) % 7)
            result = truncate(result, "days")
        else:
            # normal case
            result = truncate(result, self.unit)
        return result


    def apply_to_with_tz(self, dttm, timezone):
        result = self.apply_to(dttm)
        fold = getattr(dttm, "fold", 0)

        #naive = datetime.combine(result.date(), result.timetz().replace(tzinfo=None))
        
        naive = datetime(result.year, result.month, result.day, result.hour, result.minute, result.second)
        aware = naive.replace(tzinfo=timezone, fold=fold)
        validated = aware.astimezone(ZoneInfo("UTC")).astimezone(timezone)

        if (validated.hour, validated.minute) != (aware.hour, aware.minute):
            # Hopp tilbake én time og prøv igjen
            if naive.month > 6:
                fallback_naive = naive + timedelta(hours=1)
            else: 
                fallback_naive = naive - timedelta(hours=1)
            fallback_aware = fallback_naive.replace(tzinfo=timezone)
            validated = fallback_aware.astimezone(timezone)

        return validated




class DeltaTransformation(object):
    def __init__(self, group):
        matchdict = D_PATTERN.match(group).groupdict()
        self.mult = get_mult(matchdict.get("sign"))
        self.num = get_num(matchdict.get("num"))
        self.unit = get_unit(matchdict.get("unit_string"))

    def apply_to(self, dttm):
        return dttm + relativedelta(**{self.unit: self.mult * self.num})

    def apply_to_with_tz(self, dttm, timezone):
        return self.apply_to(dttm)


def parse(instruction):
    instr = instruction
    result = []

    while instr:
        match = HEAD_PATTERN.match(instr)
        if not match:
            raise SnapParseError("Cannot parse instruction '%s'. There is an error at '%s'" % (instruction, instr))
        group = match.group(1)

        if S_PATTERN.match(group):
            transformation = SnapTransformation(group)
        else:
            transformation = DeltaTransformation(group)

        result.append(transformation)
        instr = match.group(2)

    return result


def snap(dttm, instruction):
    """
    Args:
        instruction (string): a string that encodes 0 to n transformations of a time, i.e. "-1h@h", "@mon+2d+4h", ...
        dttm (datetime):
    Returns:
        datetime: The datetime resulting from applying all transformations to the input datetime.

    Example:
        >>> snap(datetime(2016, 1, 1, 15, 30), "-1h@h")
        datetime(2016, 1, 1, 14)
    """
    transformations = parse(instruction)
    return reduce(lambda dt, transformation: transformation.apply_to(dt), transformations, dttm)


def snap_tz(dttm, instruction, timezone):
    """This function handles timezone aware datetimes.
    Sometimes it is necessary to keep daylight saving time switches in mind.

    Args:
        instruction (string): a string that encodes 0 to n transformations of a time, i.e. "-1h@h", "@mon+2d+4h", ...
        dttm (datetime): a datetime with timezone
        timezone: a pytz timezone
    Returns:
        datetime: The datetime resulting from applying all transformations to the input datetime.

    Example:
        >>> import pytz
        >>> CET = pytz.timezone("Europe/Berlin")
        >>> dttm = CET.localize(datetime(2017, 3, 26, 3, 44)
        >>> dttm
        datetime.datetime(2017, 3, 26, 3, 44, tzinfo=<DstTzInfo 'Europe/Berlin' CEST+2:00:00 DST>)

        >>> snap_tz(dttm, "-2h@h", CET)
        datetime.datetime(2017, 3, 26, 0, 0, tzinfo=<DstTzInfo 'Europe/Berlin' CET+1:00:00 STD>)
        >>> # switch from winter to summer time!
    """
    transformations = parse(instruction)
    return reduce(lambda dt, transformation: transformation.apply_to_with_tz(dt, timezone), transformations, dttm)
