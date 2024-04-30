#######################################################################
# Copyright (c) 2019-present, Blosc Development Team <blosc@blosc.org>
# All rights reserved.
#
# This source code is licensed under a BSD-style license (found in the
# LICENSE file in the root directory of this source tree)
#######################################################################
import math

import numexpr as ne
import numpy as np
import copy
from abc import ABC, abstractmethod

import blosc2


class LazyArray(ABC):
    @abstractmethod
    def eval(self, **kwargs):
        pass

    @abstractmethod
    def __getitem__(self, item):
        pass


def fuse_operands(operands1, operands2):
    new_operands = {}
    dup_operands = {}
    new_pos = len(operands1)
    for k2, v2 in operands2.items():
        try:
            k1 = list(operands1.keys())[list(operands1.values()).index(v2)]
            # The operand is duplicated; keep track of it
            dup_operands[k2] = k1
        except ValueError:
            # The value is not among operands1, so rebase it
            new_op = f"o{new_pos}"
            new_pos += 1
            new_operands[new_op] = operands2[k2]
    return new_operands, dup_operands


def fuse_expressions(expr, new_base, dup_op):
    new_expr = ""
    skip_to_char = 0
    old_base = 0
    prev_pos = {}
    for i in range(len(expr)):
        if i < skip_to_char:
            continue
        if expr[i] == "o":
            if i > 0 and (expr[i - 1] != " " and expr[i - 1] != "("):
                # Not a variable
                new_expr += expr[i]
                continue
            # This is a variable.  Find the end of it.
            j = i + 1
            for k in range(len(expr[j:])):
                if expr[j + k] in " )[":
                    j = k
                    break
            if expr[i + j] == ")":
                j -= 1
            old_pos = int(expr[i + 1 : i + j + 1])
            old_op = f"o{old_pos}"
            if old_op not in dup_op:
                if old_pos in prev_pos:
                    # Keep track of duplicated old positions inside expr
                    new_pos = prev_pos[old_pos]
                else:
                    new_pos = old_base + new_base
                    old_base += 1
                new_expr += f"o{new_pos}"
                prev_pos[old_pos] = new_pos
            else:
                new_expr += dup_op[old_op]
            skip_to_char = i + j + 1
        else:
            new_expr += expr[i]
    return new_expr


class LazyExpr(LazyArray):
    """Class for hosting lazy expressions.

    This is not meant to be called directly from user space.

    Once the lazy expression is created, it can be evaluated via :func:`LazyExpr.eval`.
    """

    def __init__(self, new_op):
        value1, op, value2 = new_op
        if value2 is None:
            # ufunc
            if isinstance(value1, LazyExpr):
                self.expression = f"{op}({self.expression})"
            else:
                self.operands = {"o0": value1}
                self.expression = "o0" if op is None else f"{op}(o0)"
            return
        elif op in ("atan2", "pow"):
            self.operands = {"o0": value1, "o1": value2}
            self.expression = f"{op}(o0, o1)"
            return
        if isinstance(value1, int | float) and isinstance(value2, int | float):
            self.expression = f"({value1} {op} {value2})"
        elif isinstance(value2, int | float):
            self.operands = {"o0": value1}
            self.expression = f"(o0 {op} {value2})"
        elif isinstance(value1, int | float):
            self.operands = {"o0": value2}
            self.expression = f"({value1} {op} o0)"
        else:
            if value1 is value2:
                self.operands = {"o0": value1}
                self.expression = f"(o0 {op} o0)"
            elif isinstance(value1, LazyExpr) or isinstance(value2, LazyExpr):
                if isinstance(value1, LazyExpr):
                    self.expression = value1.expression
                    self.operands = {"o0": value2}
                else:
                    self.expression = value2.expression
                    self.operands = {"o0": value1}
                self.update_expr(new_op)
            else:
                # This is the very first time that a LazyExpr is formed from two operands
                # that are not LazyExpr themselves
                self.operands = {"o0": value1, "o1": value2}
                self.expression = f"(o0 {op} o1)"

    def update_expr(self, new_op):
        # We use a lot the original NDArray.__eq__ as 'is', so deactivate the overloaded one
        blosc2._disable_overloaded_equal = True
        # One of the two operands are LazyExpr instances
        value1, op, value2 = new_op
        if isinstance(value1, LazyExpr) and isinstance(value2, LazyExpr):
            # Expression fusion
            # Fuse operands in expressions and detect duplicates
            new_op, dup_op = fuse_operands(value1.operands, value2.operands)
            # Take expression 2 and rebase the operands while removing duplicates
            new_expr = fuse_expressions(value2.expression, len(value1.operands), dup_op)
            self.expression = f"({self.expression} {op} {new_expr})"
            self.operands.update(new_op)
        elif isinstance(value1, LazyExpr):
            if op == "not":
                self.expression = f"({op}{self.expression})"
            elif isinstance(value2, int | float):
                self.expression = f"({self.expression} {op} {value2})"
            else:
                try:
                    op_name = list(value1.operands.keys())[list(value1.operands.values()).index(value2)]
                except ValueError:
                    op_name = f"o{len(self.operands)}"
                    self.operands[op_name] = value2
                self.expression = f"({self.expression} {op} {op_name})"
        else:
            if isinstance(value1, int | float):
                self.expression = f"({value1} {op} {self.expression})"
            else:
                try:
                    op_name = list(value2.operands.keys())[list(value2.operands.values()).index(value1)]
                except ValueError:
                    op_name = f"o{len(self.operands)}"
                    self.operands[op_name] = value1
                if op == "[]":  # syntactic sugar for slicing
                    self.expression = f"({op_name}[{self.expression}])"
                else:
                    self.expression = f"({op_name} {op} {self.expression})"
        blosc2._disable_overloaded_equal = False
        return self

    def __add__(self, value):
        return self.update_expr(new_op=(self, "+", value))

    def __iadd__(self, other):
        return self.update_expr(new_op=(self, "+", other))

    def __radd__(self, value):
        return self.update_expr(new_op=(value, "+", self))

    def __sub__(self, value):
        return self.update_expr(new_op=(self, "-", value))

    def __isub__(self, value):
        return self.update_expr(new_op=(self, "-", value))

    def __rsub__(self, value):
        return self.update_expr(new_op=(value, "-", self))

    def __mul__(self, value):
        return self.update_expr(new_op=(self, "*", value))

    def __imul__(self, value):
        return self.update_expr(new_op=(self, "*", value))

    def __rmul__(self, value):
        return self.update_expr(new_op=(value, "*", self))

    def __truediv__(self, value):
        return self.update_expr(new_op=(self, "/", value))

    def __itruediv__(self, value):
        return self.update_expr(new_op=(self, "/", value))

    def __rtruediv__(self, value):
        return self.update_expr(new_op=(value, "/", self))

    def __and__(self, value):
        return self.update_expr(new_op=(self, "and", value))

    def __rand__(self, value):
        return self.update_expr(new_op=(value, "and", self))

    def __or__(self, value):
        return self.update_expr(new_op=(self, "or", value))

    def __ror__(self, value):
        return self.update_expr(new_op=(value, "or", self))

    def __invert__(self):
        return self.update_expr(new_op=(self, "not", None))

    def __pow__(self, value):
        return self.update_expr(new_op=(self, "**", value))

    def __rpow__(self, value):
        return self.update_expr(new_op=(value, "**", self))

    def __ipow__(self, value):
        return self.update_expr(new_op=(self, "**", value))

    def eval(self, item=None, **kwargs) -> blosc2.NDArray:
        """Evaluate the lazy expression in self.

        Parameters
        ----------
        item: slice, list of slices, optional
            If not None, only the chunks that intersect with the slices
            in items will be evaluated.
        kwargs: dict, optional
            Keyword arguments that are supported by the :func:`empty` constructor.

        Returns
        -------
        :ref:`NDArray`
            The output array.
        """
        shape, dtype, equal_chunks, equal_blocks, has_padding = validate_inputs(self.operands)
        nelem = np.prod(shape)
        if item is not None and item != slice(None, None, None):
            return evaluate_slices(self.expression, self.operands, _slice=item, **kwargs)
        if nelem <= 10_000:  # somewhat arbitrary threshold
            out = evaluate_incache(self.expression, self.operands, **kwargs)
        elif equal_chunks and equal_blocks:
            getitem = kwargs.get("_getitem", False)
            if getitem and has_padding:
                # We need to evaluate the expression via NDArray because the logic
                # for NDim padding is in the NDArray object
                kwargs.pop("_getitem")
            out = evaluate_chunks(self.expression, self.operands, **kwargs)
        else:
            out = evaluate_slices(self.expression, self.operands, **kwargs)
        return out

    def __getitem__(self, item):
        if item == Ellipsis:
            item = slice(None, None, None)
        ndarray = self.eval(item=item, **{"_getitem": True})
        full_data = item is None or item == slice(None, None, None) or item == Ellipsis
        return ndarray[item] if not full_data else ndarray[:]

    def __str__(self):
        expression = f"{self.expression}"
        return expression


def validate_inputs(inputs: dict) -> tuple:
    """Validate the inputs for the expression."""
    if len(inputs) == 0:
        raise ValueError(
            "You need to pass at least one array.  Use blosc2.empty() if values are not really needed."
        )
    inputs = list(inputs.values())
    first_input = inputs[0]
    equal_chunks = True
    equal_blocks = True
    if first_input.blocks[1:] != first_input.chunks[1:]:
        # For some reason, the trailing dimensions not being the same is not supported in fast path
        equal_blocks = False
    for input_ in inputs[1:]:
        if first_input.shape != input_.shape:
            raise ValueError("Inputs should have the same shape")
        if first_input.chunks != input_.chunks:
            equal_chunks = False
        if first_input.blocks != input_.blocks:
            equal_blocks = False
        # TODO: see why we need this constraint for avoiding the fast path
        if first_input.blocks[1:] != input_.chunks[1:]:
            # For some reason, the trailing dimensions not being the same is not supported in fast path
            equal_blocks = False
    has_padding = False
    # Check if there is padding for more than 1-dim operands (1-dim is supported in getitem mode)
    if equal_blocks and equal_chunks and len(first_input.shape) > 1:
        has_padding = any([c % b != 0 for c, b in zip(first_input.chunks, first_input.blocks, strict=True)])

    return first_input.shape, first_input.dtype, equal_chunks, equal_blocks, has_padding


def evaluate_incache(expression: str, operands: dict, **kwargs) -> blosc2.NDArray | np.ndarray:
    """Evaluate the expression in chunks of operands.

    This can be used when operands fit in CPU cache.

    Parameters
    ----------
    expression: str
        The expression to evaluate.
    operands: dict
        A dictionary with the operands.
    kwargs: dict, optional
        Keyword arguments that are supported by the :func:`empty` constructor.

    Returns
    -------
    :ref:`NDArray`
        The output array.
    """
    # Convert NDArray objects to numpy arrays
    numpy_operands = {key: value[:] for key, value in operands.items()}
    # Evaluate the expression using numexpr
    result = ne.evaluate(expression, numpy_operands)
    getitem = kwargs.pop("_getitem", False)
    if getitem:
        return result
    # Convert the resulting numpy array back to an NDArray
    return blosc2.asarray(result, **kwargs)


def evaluate_chunks(expression: str, operands: dict, **kwargs) -> blosc2.NDArray:
    """Evaluate the expression in chunks of operands.

    This can be used when the expression is too big to fit in CPU cache.

    Parameters
    ----------
    expression: str
        The expression to evaluate.
    operands: dict
        A dictionary with the operands.
    kwargs: dict, optional
        Keyword arguments that are supported by the :func:`empty` constructor.

    Returns
    -------
    :ref:`NDArray`
        The output array.
    """
    getitem = kwargs.pop("_getitem", False)
    operand = operands["o0"]
    shape = operand.shape
    chunks = operand.chunks
    out = None
    for info in operands["o0"].iterchunks_info():
        # Iterate over the operands and get the chunks
        chunk_operands = {}
        is_special = info.special
        if is_special == blosc2.SpecialValue.ZERO:
            # print("Zero!")
            pass

        slice_, chunks_ = None, None  # silence linter
        if getitem:
            # Calculate the shape of the (chunk) slice_ (specially at the end of the array)
            slice_ = tuple(
                slice(c * s, min((c + 1) * s, shape[i]))
                for i, (c, s) in enumerate(zip(info.coords, chunks, strict=True))
            )
            chunks_ = tuple(s.stop - s.start for s in slice_)

        for key, value in operands.items():
            lazychunk = value.schunk.get_lazychunk(info.nchunk)
            special = lazychunk[15] >> 4
            if is_special == blosc2.SpecialValue.ZERO and special == blosc2.SpecialValue.ZERO:
                # TODO: If both are zeros, we can skip the computation under some conditions
                # print("Skipping chunk")
                # continue
                pass
            if getitem:
                if chunks_ != chunks:
                    # The chunk is not a full one, so we need to fetch the valid data
                    npbuff = value[slice_]
                else:
                    # Fast path for full chunks
                    buff = value.schunk.decompress_chunk(info.nchunk)
                    bsize = value.dtype.itemsize * math.prod(chunks_)
                    npbuff = np.frombuffer(buff[:bsize], dtype=value.dtype).reshape(chunks_)
            else:
                buff = value.schunk.decompress_chunk(info.nchunk)
                # We don't want to reshape the buffer (to better handle padding)
                npbuff = np.frombuffer(buff, dtype=value.dtype)
            chunk_operands[key] = npbuff
        if out is None:
            # Evaluate the expression using chunks of operands
            result = ne.evaluate(expression, chunk_operands)
            if getitem:
                out = np.empty(shape, dtype=result.dtype)
                out[slice_] = result
            else:
                # Due to padding, it is critical to have the same chunks and blocks as the operands
                out = blosc2.empty(
                    shape, chunks=operand.chunks, blocks=operand.blocks, dtype=result.dtype, **kwargs
                )
                out.schunk.update_data(info.nchunk, result, copy=False)
        elif getitem:
            # Assign the result to the output array (avoiding a memory copy)
            ne.evaluate(expression, chunk_operands, out=out[slice_])
        else:
            # Update the output array with the result
            result = ne.evaluate(expression, chunk_operands)
            out.schunk.update_data(info.nchunk, result, copy=False)
    return out


def evaluate_slices(expression: str, operands: dict, _slice=None, **kwargs) -> blosc2.NDArray:
    """Evaluate the expression in chunks of operands.

    This can be used when the operands in the expression have different chunk shapes.
    Also, it can be used when only a slice of the output array is needed.

    Parameters
    ----------
    expression: str
        The expression to evaluate.
    operands: dict
        A dictionary with the operands.
    _slice: slice, list of slices, optional
        If not None, only the chunks that intersect with this slice
        will be evaluated.
    kwargs: dict, optional
        Keyword arguments that are supported by the :func:`empty` constructor.

    Returns
    -------
    :ref:`NDArray`
        The output array.
    """
    getitem = kwargs.pop("_getitem", False)
    operand = operands["o0"]
    shape = operand.shape
    chunks = operand.chunks
    out = None
    for info in operand.iterchunks_info():
        # Iterate over the operands and get the chunks
        chunk_operands = {}
        coords = info.coords
        slice_ = [slice(c * s, (c + 1) * s) for c, s in zip(coords, chunks, strict=True)]
        # Check whether current slice_ intersects with _slice
        if _slice is not None:
            intersects = do_slices_intersect(_slice, slice_)
            if not intersects:
                continue
        if len(slice_) == 1:
            slice_ = slice_[0]
        else:
            slice_ = tuple(slice_)
        # Get the slice of each operand
        for key, value in operands.items():
            chunk_operands[key] = value[slice_]

        # Evaluate the expression using chunks of operands
        result = ne.evaluate(expression, chunk_operands)
        if out is None:
            if getitem:
                out = np.empty(shape, dtype=result.dtype)
            else:
                # Let's use the same chunks as the first operand (it could have been automatic too)
                out = blosc2.empty(shape, chunks=chunks, dtype=result.dtype, **kwargs)
        out[slice_] = result

    return out


def do_slices_intersect(slice1, slice2):
    """
    Check whether two slices intersect.

    Parameters
    ----------
    slice1: slice, list of slices
        The first slice
    slice2: slice, list of slices
        The second slice

    Returns
    -------
    bool
        Whether the slices intersect
    """
    # Ensure the slices are in list format
    if not isinstance(slice1, list):
        slice1 = [slice1]
    if not isinstance(slice2, list):
        slice2 = [slice2]

    # Pad the shorter slice list with full slices (:)
    while len(slice1) < len(slice2):
        slice1.append(slice(None))
    while len(slice2) < len(slice1):
        slice2.append(slice(None))

    # Check each dimension for intersection
    for s1, s2 in zip(slice1, slice2, strict=True):
        if s1.start is not None and s2.stop is not None and s1.start >= s2.stop:
            return False
        if s1.stop is not None and s2.start is not None and s1.stop <= s2.start:
            return False

    return True


if __name__ == "__main__":
    from time import time

    # Create initial containers
    na1 = np.linspace(0, 10, 10_000_000, dtype=np.float64)
    a1 = blosc2.asarray(na1)
    na2 = np.copy(na1)
    a2 = blosc2.asarray(na2)
    na3 = np.copy(na1)
    a3 = blosc2.asarray(na3)
    na4 = np.copy(na1)
    a4 = blosc2.asarray(na4)
    # Interesting slice
    # sl = None
    sl = slice(0, 10_000)
    # Create a simple lazy expression
    expr = a1 + a2
    print(expr)
    t0 = time()
    nres = na1 + na2
    print(f"Elapsed time (numpy, [:]): {time() - t0:.3f} s")
    t0 = time()
    nres = ne.evaluate("na1 + na2")
    print(f"Elapsed time (numexpr, [:]): {time() - t0:.3f} s")
    nres = nres[sl] if sl is not None else nres
    t0 = time()
    res = expr.eval(item=sl)
    print(f"Elapsed time (evaluate): {time() - t0:.3f} s")
    res = res[sl] if sl is not None else res[:]
    t0 = time()
    res2 = expr[sl]
    print(f"Elapsed time (getitem): {time() - t0:.3f} s")
    np.testing.assert_allclose(res, nres)
    np.testing.assert_allclose(res2, nres)

    # Complex lazy expression
    expr = blosc2.tan(a1) * (blosc2.sin(a2) * blosc2.sin(a2) + blosc2.cos(a3)) + (blosc2.sqrt(a4) * 2)
    # expr = blosc2.sin(a1) + 2 * a1 + 1
    expr += 2
    print(expr)
    t0 = time()
    nres = np.tan(na1) * (np.sin(na2) * np.sin(na2) + np.cos(na3)) + (np.sqrt(na4) * 2) + 2
    # nres = np.sin(na1[:]) + 2 * na1[:] + 1 + 2
    print(f"Elapsed time (numpy, [:]): {time() - t0:.3f} s")
    t0 = time()
    nres = ne.evaluate("tan(na1) * (sin(na2) * sin(na2) + cos(na3)) + (sqrt(na4) * 2) + 2")
    print(f"Elapsed time (numexpr, [:]): {time() - t0:.3f} s")
    nres = nres[sl] if sl is not None else nres
    t0 = time()
    res = expr.evaluate(sl)
    print(f"Elapsed time (evaluate): {time() - t0:.3f} s")
    res = res[sl] if sl is not None else res[:]
    t0 = time()
    res2 = expr[sl]
    print(f"Elapsed time (getitem): {time() - t0:.3f} s")
    np.testing.assert_allclose(res, nres)
    np.testing.assert_allclose(res2, nres)
    print("Everything is working fine")


class LazyUDF(LazyArray):
    def _validate_inputs(self, inputs):
        inputs_ = []
        for obj in inputs:
            if not isinstance(obj, np.ndarray | blosc2.NDArray) and not np.isscalar(obj):
                try:
                    obj = np.asarray(obj)
                except:
                    print(
                        "Inputs not being np.ndarray, NDArray or Python scalar objects"
                        " should be convertible to np.ndarray."
                    )
                    raise
            inputs_.append(obj)
        return inputs_

    def __init__(self, func, inputs, dtype, **kwargs):
        # After this, all the inputs should be np.ndarray or NDArray objects
        self.inputs = self._validate_inputs(inputs)
        self.shape = None
        # Get res shape
        for obj in self.inputs:
            if isinstance(obj, np.ndarray | blosc2.NDArray):
                self.shape = obj.shape
                break
        if self.shape is None:
            raise NotImplementedError("If all operands are Python scalars, use python, numpy or numexpr")

        self.kwargs = kwargs
        self.dtype = dtype
        self.func = func

        # Prepare internal array for __getitem__

        # Deep copy the kwargs to evict modifying them
        kwargs_getitem = copy.deepcopy(self.kwargs)
        # Cannot use multithreading when applying a postfilter, dparams['nthreads'] ignored
        dparams = kwargs_getitem.get("dparams", {})
        if isinstance(dparams, dict):
            dparams["nthreads"] = 1
        else:
            raise ValueError("dparams should be a dictionary")
        kwargs_getitem["dparams"] = dparams
        self.res_getitem = blosc2.empty(self.shape, self.dtype, **kwargs_getitem)
        self.res_getitem._set_postf_udf(self.func, id(self.inputs))

    def eval(self, **kwargs):
        """
        Get a :ref:`NDArray <NDArray>` containing the evaluation of the :ref:`LazyUDF <LazyUDF>`.

        Returns
        -------
        out: :ref:`NDArray <NDArray>`
            A :ref:`NDArray <NDArray>` containing the result of evaluating the
            :ref:`LazyUDF <LazyUDF>`.

        Notes
        -----
        Because this calls a Python function when compressing, the `cparams[nthreads]` is set to
        one during the evaluation. After it, the original `cparams[nthreads]` value is restored.

        """
        # Get kwargs
        if kwargs is None:
            kwargs = {}
        aux_kwargs = copy.deepcopy(self.kwargs)  # Do copy to evict modifying the original parameters
        # Update is not recursive
        cparams = aux_kwargs.get('cparams', {})
        cparams.update(kwargs.get('cparams', {}))
        aux_kwargs['cparams'] = cparams
        dparams = aux_kwargs.get('dparams', {})
        dparams.update(kwargs.get('dparams', {}))
        aux_kwargs['dparams'] = dparams
        _ = kwargs.pop('cparams', None)
        _ = kwargs.pop('dparams', None)
        urlpath = kwargs.get('urlpath', None)
        if urlpath is not None and urlpath == aux_kwargs.get('urlpath', None):
            raise ValueError("Cannot use same urlpath for LazyArray and eval NDArray")
        _ = aux_kwargs.pop('urlpath', None)
        aux_kwargs.update(kwargs)

        # Cannot use multithreading when applying a prefilter, save nthreads to set them
        # after the evaluation
        cparams = aux_kwargs.get("cparams", {})
        if isinstance(cparams, dict):
            self._cnthreads = cparams.get("nthreads", blosc2.cparams_dflts["nthreads"])
            cparams["nthreads"] = 1
        else:
            raise ValueError("cparams should be a dictionary")
        aux_kwargs["cparams"] = cparams

        res_eval = blosc2.empty(self.shape, self.dtype, **aux_kwargs)
        res_eval._set_pref_udf(self.func, id(self.inputs))

        aux = np.empty(res_eval.shape, res_eval.dtype)
        res_eval[...] = aux
        res_eval.schunk.remove_prefilter(self.func.__name__)
        res_eval.schunk.cparams["nthreads"] = self._cnthreads

        return res_eval

    def __getitem__(self, item):
        """
        Evaluate a slice of the :ref:`LazyUDF <LazyUDF>`.

        Parameters
        ----------
        item: slice
            The slice of the :ref:`LazyUDF <LazyUDF>` to evaluate.

        Returns
        -------
        out: NumPy.ndarray
            The result of evaluating the slice of the :ref:`LazyUDF <LazyUDF>`
            as a NumPy.ndarray.

        """

        return self.res_getitem[item]


def lazyudf(func, inputs, dtype, **kwargs):
    """
    Get a LazyUDF from a python user-defined function.

    Parameters
    ----------
    func: Python function
        User defined function to apply to each block.
    inputs: Sequence of np.ndarray or :ref:`NDArray <NDArray>`
        The supported inputs are NumPy.ndarray, Python scalars, and :ref:`NDArray <NDArray>`.
    dtype: np.dtype
        The resulting ndarray dtype in NumPy format.
    kwargs: dict, optional
        Keyword arguments that are supported by the :func:`empty` constructor.
        These arguments will be used by the `__getitem__` and `eval` methods. The
        last one will ignore the `urlpath` parameter to `lazyudf` if passed.

    Returns
    -------
    out: :ref:`LazyUDF <LazyUDF>`
        A :ref:`LazyUDF <LazyUDF>` is returned.

    Notes
    -----
    * If `urlpath` or `contiguous` are passed as kwargs,
    * Since the

    """
    return LazyUDF(func, inputs, dtype, **kwargs)
