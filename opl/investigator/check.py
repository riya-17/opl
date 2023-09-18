import collections
import inspect
import logging
import statistics


def _count_deviation(value, lower_boundary, upper_boundary):
    if lower_boundary <= value <= upper_boundary:
        return None
    else:
        dist = min(abs(lower_boundary - value), abs(value - upper_boundary))
        try:
            frac = dist / abs(upper_boundary - lower_boundary)
        except ZeroDivisionError:
            frac = 1
        logging.debug(
            f"_count_deviation({value}, {lower_boundary}, {upper_boundary}): dist={dist} frac={frac}"
        )
        return frac


def calculate_lower_upper_boundary(data, mean, comparator):
    """Returns the calculated lower and upper boundary of the data

    Args:
        data: collected history data
        mean: mean of the collected history data
        comparator: defines the type of comparison to be done

    Returns:
        tuple with lower and upper boundary
    """
    assert isinstance(data, list), "Data provided have to be a list"
    lower_boundary = float(mean - (mean - min(data)))
    upper_boundary = float(mean + (max(data) - mean))
    if comparator == "lte_max":
        return (float("-inf"), upper_boundary)
    elif comparator == "gte_min":
        return (lower_boundary, float("inf"))
    else:
        return (lower_boundary, upper_boundary)


def _check_by_min_max(data, value, comparator):
    """Checks the value range using lower and upper boundary.
    If the value is within given range it is a PASS else a FAIL

    Args:
        data: collected history data
        value: value to be checked against
        comparator: defines the type of comparison to be done

    Returns:
        Boolean value
    """
    logging.debug(f"data={data} and value={value}")
    mean = statistics.mean(data)
    lower_boundary, upper_boundary = calculate_lower_upper_boundary(
        data, mean, comparator
    )
    logging.info(
        f"value={value}, data len={len(data)} mean={mean:.03f}, i.e. boundaries={lower_boundary:.03f}--{upper_boundary:.03f}"
    )
    info = collections.OrderedDict(
        [
            ("method", inspect.stack()[1][3]),
            ("value", value),
            ("data len", len(data)),
            ("data mean", mean),
            ("data min", float(min(data))),
            ("data max", float(max(data))),
            ("lower_boundary", lower_boundary),
            ("upper_boundary", upper_boundary),
        ]
    )
    if comparator == "lte_max":
        return value <= upper_boundary, info
    elif comparator == "gte_min":
        return lower_boundary <= value, info
    else:
        return lower_boundary <= value <= upper_boundary, info


def _check_by_stdev(data, value, num_deviations):
    logging.debug(f"data={data} and value={value}")
    mean = statistics.mean(data)
    stdev = statistics.stdev(data)
    acceptable_deviation = stdev * num_deviations
    lower_boundary = float(mean - acceptable_deviation)
    upper_boundary = float(mean + acceptable_deviation)
    logging.info(
        f"value={value}, data len={len(data)} mean={mean:.03f}, stdev={stdev:.03f}, boundaries={lower_boundary:.03f}--{upper_boundary:.03f}"
    )
    info = collections.OrderedDict(
        [
            ("method", inspect.stack()[1][3]),
            ("value", value),
            ("data len", len(data)),
            ("data mean", mean),
            ("data stdev", stdev),
            ("data min", float(min(data))),
            ("data max", float(max(data))),
            ("lower_boundary", lower_boundary),
            ("upper_boundary", upper_boundary),
        ]
    )
    return lower_boundary <= value <= upper_boundary, info


def check_by_iqr(data, value):
    """Checks if the current value is within the interquartile range of the previous values"""
    logging.debug(f"data={data} and value={value}")
    mean = statistics.mean(data)
    quantiles = statistics.quantiles(data)
    lower_boundary = float(quantiles[0])
    upper_boundary = float(quantiles[2])
    logging.info(
        f"value={value}, data len={len(data)} mean={mean:.03f}, boundaries={lower_boundary:.03f}--{upper_boundary:.03f}"
    )
    info = collections.OrderedDict(
        [
            ("method", inspect.stack()[0][3]),
            ("value", value),
            ("data len", len(data)),
            ("data mean", mean),
            ("data quantiles", quantiles),
            ("data min", float(min(data))),
            ("data max", float(max(data))),
            ("lower_boundary", lower_boundary),
            ("upper_boundary", upper_boundary),
        ]
    )
    return lower_boundary <= value <= upper_boundary, info


def check_by_min_max_0_1(data, value):
    """Checks if the current value is within the min/max range of previous values"""
    return _check_by_min_max(data, value, None)


def check_by_lte_max(data, value):
    """Checks if the current value is less than max range of previous values"""
    return _check_by_min_max(data, value, "lte_max")


def check_by_gte_min(data, value):
    """Checks if the current value is more than min range of previous values"""
    return _check_by_min_max(data, value, "gte_min")


def check_by_stdev_1(data, value):
    """Checks if the current value is within 1 standard deviations of the mean of previous values"""
    return _check_by_stdev(data, value, 1)


def check_by_stdev_2(data, value):
    """Checks if the current value is within 2 standard deviations of the mean of previous values"""
    return _check_by_stdev(data, value, 2)


def check_by_stdev_3(data, value):
    """Checks if the current value is within 2 standard deviations of the mean of previous values"""
    return _check_by_stdev(data, value, 3)


def check(methods, data, value, description="N/A", verbose=True):
    assert value is not None, "Value to check should not be None"

    if methods == []:
        methods = ["check_by_min_max_0_1"]
    for method in methods:
        assert method in globals(), f"Check method '{method}' not defined"

    results = []
    info_all = []
    for method in methods:
        result, info = globals()[method](data, value)
        results.append(result)
        logging.info(f"{method} value {value} returned {'PASS' if result else 'FAIL'}")

        info_full = collections.OrderedDict()
        info_full["description"] = description
        info_full["result"] = "PASS" if result else "FAIL"
        info_full.update(info)
        info_full["deviation"] = _count_deviation(
            value, info["lower_boundary"], info["upper_boundary"]
        )
        info_all.append(info_full)
    return results, info_all
