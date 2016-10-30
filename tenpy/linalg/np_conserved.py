r"""A module to handle charge conservation in tensor networks.

A detailed introduction (including notations) can be found in :doc:`../IntroNpc`.

This module `np_conserved` implements an class :class:`Array`
designed to make use of charge conservation in tensor networks.
The idea is that the `Array` class is used in a fashion very similar to
the `numpy.ndarray`, e.g you can call the functions :func:`tensordot` or :func:`svd`
(of this module) on them.
The structure of the algorithms (as DMRG) is thus the same as with basic numpy ndarrays.

Internally, an :class:`Array` saves charge meta data to keep track of blocks which are nonzero.
All possible operations (e.g. tensordot, svd, ...) on such arrays preserve the total charge
structure. In addition, these operations make use of the charges to figure out which of the blocks
it hase to use/combine - this is the basis for the speed-up.


See also
--------
:mod:`tenpy.linalg.charges` : Implementation of :class:`~tenpy.linalg.charges.ChargeInfo`
and :class:`~tenpy.linalg.charges.LegCharge` with additional documentation.


.. todo ::
   function listing,
   write example section
"""
# Examples
# --------
# >>> import numpy as np
# >>> import tenpy.linalg.np_conserved as npc
# >>> Sz = np.array([[0., 1.], [1., 0.]])
# >>> Sz_c = npc.Array.from_ndarray_trivial(Sz)  # convert to npc array with trivial charge
# >>> Sz_c
# <npc.Array shape=(2, 2)>
# >>> sx = npc.ndarray.from_ndarray([[0., 1.], [1., 0.]])  # trivial charge conservation
# >>> b = npc.ndarray.from_ndarray([[0., 1.], [1., 0.]])  # trivial charge conservation
# >>>
# >>> print a[0, -1]
# >>> c = npc.tensordot(a, b, axes=([1], [0]))

from __future__ import division

import numpy as np
import scipy as sp
import copy as copy_
import warnings
import itertools

# import public API from charges
from .charges import (QDTYPE, ChargeInfo, LegCharge, LegPipe, reverse_sort_perm)
from . import charges  # for private functions

from ..tools.math import toiterable

#: A cutoff to ignore machine precision rounding errors when determining charges
QCUTOFF = np.finfo(np.float64).eps * 10


class Array(object):
    r"""A multidimensional array (=tensor) for using charge conservation.

    An `Array` represents a multi-dimensional tensor,
    together with the charge structure of its legs (for abelian charges).
    Further information can be found in :doc:`../IntroNpc`.

    The default :meth:`__init__` (i.e. ``Array(...)``) does not insert any data,
    and thus yields an Array 'full' of zeros, equivalent to :func:`zeros()`.
    Further, new arrays can be created with one of :meth:`from_ndarray_trivial`,
    :meth:`from_ndarray`, or :meth:`from_npfunc`, and of course by copying/tensordot/svd etc.

    In-place methods are indicated by a name starting with ``i``.
    (But `is_completely_blocked` is not inplace...)


    Parameters
    ----------
    chargeinfo : :class:`~tenpy.linalg.charges.ChargeInfo`
        the nature of the charge, used as self.chinfo.
    legs : list of :class:`~tenpy.linalg.charges.LegCharge`
        the leg charges for each of the legs.
    dtype : type or string
        the data type of the array entries. Defaults to np.float64.
    qtotal : 1D array of QDTYPE
        the total charge of the array. Defaults to 0.

    Attributes
    ----------
    rank
    size
    stored_blocks
    shape : tuple(int)
        the number of indices for each of the legs
    dtype : np.dtype
        the data type of the entries
    chinfo : :class:`~tenpy.linalg.charges.ChargeInfo`
        the nature of the charge
    qtotal : 1D array
        the total charge of the tensor.
    legs : list of :class:`~tenpy.linalg.charges.LegCharge`
        the leg charges for each of the legs.
    labels : dict (string -> int)
        labels for the different legs
    _data : list of arrays
        the actual entries of the tensor
    _qdata : 2D array (len(_data), rank), dtype np.intp
        for each of the _data entries the qind of the different legs.
    _qdata_sorted : Bool
        whether self._qdata is lexsorted. Defaults to `True`,
        but *must* be set to `False` by algorithms changing _qdata.
    """

    def __init__(self, chargeinfo, legcharges, dtype=np.float64, qtotal=None):
        """see help(self)"""
        self.chinfo = chargeinfo
        self.legs = list(legcharges)
        self._set_shape()
        self.dtype = np.dtype(dtype)
        self.qtotal = self.chinfo.make_valid(qtotal)
        self.labels = {}
        self._data = []
        self._qdata = np.empty((0, self.rank), dtype=np.intp)
        self._qdata_sorted = True
        self.test_sanity()

    def copy(self, deep=False):
        """Return a (deep or shallow) copy of self.

        **Both** deep and shallow copies will share ``chinfo`` and the `LegCharges` in ``legs``.

        In contrast to a deep copy, the shallow copy will also share the tensor entries,
        namely the *same* instances of ``_qdata`` and ``_data`` and ``labels``
        (and other 'immutable' properties like the shape or dtype).

        .. note ::

            Shallow copies are *not* recommended unless you know the consequences!
            See the following examples illustrating some of the pitfalls.

        Examples
        --------
        Be (very!) careful when making non-deep copies: In the following example,
        the original `a` is changed if and only if the corresponding block existed in `a` before.
        >>> b = a.copy(deep=False)  # shallow copy
        >>> b[1, 2] = 4.

        Other `inplace` operations might have no effect at all (although we don't guarantee that):

        >>> a *= 2  # has no effect on `b`
        >>> b.iconj()  # nor does this change `a`
        """
        if deep:
            cp = copy_.deepcopy(self)
        else:
            cp = copy_.copy(self)
            # some things should be copied even for shallow copies
            cp.qtotal = cp.qtotal.copy()
            cp.labels = cp.labels.copy()
        # even deep copies can share chargeinfo and legs
        cp.chinfo = self.chinfo  # same instance
        cp.legs = self.legs[:]  # copied list with same instances of legs
        return cp

    @classmethod
    def from_ndarray_trivial(cls, data_flat, dtype=np.float64):
        """convert a flat numpy ndarray to an Array with trivial charge conservation.

        Parameters
        ----------
        data_flat : array_like
            the data to be converted to a Array
        dtype : type | string
            the data type of the array entries. Defaults to ``np.float64``.

        Returns
        -------
        res : :class:`Array`
            an Array with data of data_flat
        """
        data_flat = np.array(data_flat, dtype)
        chinfo = ChargeInfo()
        legs = [LegCharge.from_trivial(s, chinfo) for s in data_flat.shape]
        res = cls(chinfo, legs, dtype)
        res._data = [data_flat]
        res._qdata = np.zeros((1, res.rank), np.intp)
        res._qdata_sorted = True
        res.test_sanity()
        return res

    @classmethod
    def from_ndarray(cls,
                     data_flat,
                     chargeinfo,
                     legcharges,
                     dtype=np.float64,
                     qtotal=None,
                     cutoff=None):
        """convert a flat (numpy) ndarray to an Array.

        Parameters
        ----------
        data_flat : array_like
            the flat ndarray which should be converted to a npc `Array`.
            The shape has to be compatible with legcharges.
        chargeinfo : ChargeInfo
            the nature of the charge
        legcharges : list of LegCharge
            a LegCharge for each of the legs.
        dtype : type | string
            the data type of the array entries. Defaults to np.float64.
        qtotal : None | charges
            the total charge of the new array.
        cutoff : float
            Blocks with ``np.max(np.abs(block)) > cutoff`` are considered as zero.
            Defaults to :data:`QCUTOFF`.

        Returns
        -------
        res : :class:`Array`
            an Array with data of `data_flat`.

        See also
        --------
        detect_ndarray_qtotal : used to detect the total charge of the flat array.
        """
        if cutoff is None:
            cutoff = QCUTOFF
        res = cls(chargeinfo, legcharges, dtype, qtotal)  # without any data
        data_flat = np.asarray(data_flat, dtype=res.dtype)
        if res.shape != data_flat.shape:
            raise ValueError("Incompatible shapes: legcharges {0!s} vs flat {1!s} ".format(
                res.shape, data_flat.shape))
        if qtotal is None:
            res.qtotal = qtotal = res.detect_ndarray_qtotal(data_flat, cutoff)
        data = []
        qdata = []
        for qindices in res._iter_all_blocks():
            sl = res._get_block_slices(qindices)
            if np.all(res._get_block_charge(qindices) == qtotal):
                data.append(np.array(data_flat[sl], dtype=res.dtype))  # copy data
                qdata.append(qindices)
            elif np.any(np.abs(data_flat[sl]) > cutoff):
                warnings.warn("flat array has non-zero entries in blocks incompatible with charge")
        res._data = data
        res._qdata = np.array(qdata, dtype=np.intp).reshape((len(qdata), res.rank))
        res._qdata_sorted = True
        res.test_sanity()
        return res

    @classmethod
    def from_func(cls,
                  func,
                  chargeinfo,
                  legcharges,
                  dtype=np.float64,
                  qtotal=None,
                  func_args=(),
                  func_kwargs={},
                  shape_kw=None):
        """Create an Array from a numpy func.

        This function creates an array and fills the blocks *compatible* with the charges
        using `func`, where `func` is a function returning a `array_like` when given a shape,
        e.g. one of ``np.ones`` or ``np.random.standard_normal``.

        Parameters
        ----------
        func : callable
            a function-like object which is called to generate the data blocks.
            We expect that `func` returns a flat array of the given `shape` convertible to `dtype`.
            If no `shape_kw` is given, it is called like ``func(shape, *fargs, **fkwargs)``,
            otherwise as ``func(*fargs, `shape_kw`=shape, **fkwargs)``.
            `shape` is a tuple of int.
        chargeinfo : ChargeInfo
            the nature of the charge
        legcharges : list of LegCharge
            a LegCharge for each of the legs.
        dtype : type | string
            the data type of the output entries. Defaults to np.float64.
            Note that this argument is not given to func, but rather a type conversion
            is performed afterwards. You might want to set a `dtype` in `func_kwargs` as well.
        qtotal : None | charges
            the total charge of the new array. Defaults to charge 0.
        func_args : iterable
            additional arguments given to `func`
        func_kwargs : dict
            additional keyword arguments given to `func`
        shape_kw : None | str
            If given, the keyword with which shape is given to `func`.

        Returns
        -------
        res : :class:`Array`
            an Array with blocks filled using `func`.
        """
        res = cls(chargeinfo, legcharges, dtype, qtotal)  # without any data yet.
        data = []
        qdata = []
        for qindices in res._iter_all_blocks():
            if np.any(res._get_block_charge(qindices) != res.qtotal):
                continue
            shape = res._get_block_shape(qindices)
            if shape_kw is None:
                block = func(shape, *func_args, **func_kwargs)
            else:
                kws = func_kwargs.copy()
                kws[shape_kw] = shape
                block = func(*func_args, **kws)
            block = np.asarray(block, dtype=res.dtype)
            data.append(block)
            qdata.append(qindices)
        res._data = data
        res._qdata = np.array(qdata, dtype=np.intp).reshape((len(qdata), res.rank))
        res._qdata_sorted = True  # _iter_all_blocks is in lexiographic order
        res.test_sanity()
        return res

    def zeros_like(self):
        """return a shallow copy of self with only zeros as entries, containing no `_data`"""
        res = self.copy(deep=False)
        res._data = []
        res._qdata = np.empty((0, res.rank), dtype=np.intp)
        res._qdata_sorted = True
        return res

    def test_sanity(self):
        """Sanity check. Raises ValueErrors, if something is wrong."""
        if self.shape != tuple([lc.ind_len for lc in self.legs]):
            raise ValueError("shape mismatch with LegCharges\n self.shape={0!s} != {1!s}".format(
                self.shape, tuple([lc.ind_len for lc in self.legs])))
        if any([self.dtype != d.dtype for d in self._data]):
            raise ValueError("wrong dtype: {0!s} vs\n {1!s}".format(
                self.dtype, [self.dtype != d.dtype for d in self._data]))
        for l in self.legs:
            l.test_sanity()
            if l.chinfo != self.chinfo:
                raise ValueError("leg has different ChargeInfo:\n{0!s}\n vs {1!s}".format(
                    l.chinfo, self.chinfo))
        if self._qdata.shape != (self.stored_blocks, self.rank):
            raise ValueError("_qdata shape wrong")
        if self._qdata.dtype != np.intp:
            raise ValueError("wront dtype of _qdata")
        if np.any(self._qdata < 0) or np.any(self._qdata >= [l.block_number for l in self.legs]):
            raise ValueError("invalid qind in _qdata")
        if self._qdata_sorted:
            perm = np.lexsort(self._qdata.T)
            if np.any(perm != np.arange(len(perm))):
                raise ValueError("_qdata_sorted == True, but _qdata is not sorted")

    # properties ==============================================================

    @property
    def rank(self):
        """the number of legs"""
        return len(self.shape)

    @property
    def size(self):
        """the number of dtype-objects stored"""
        return np.sum([t.size for t in self._data], dtype=np.int_)

    @property
    def stored_blocks(self):
        """the number of (non-zero) blocks stored in self._data"""
        return len(self._data)

    # labels ==================================================================

    def get_leg_index(self, label):
        """translate a leg-index or leg-label to a leg-index.

        Parameters
        ----------
        label : int | string
            eather the leg-index directly or a label (string) set before.

        Returns
        -------
        leg_index : int
            the index of the label

        See also
        --------
        get_leg_indices : calls get_leg_index for a list of labels
        set_leg_labels : set the labels of different legs.
        """
        res = self.labels.get(label, label)
        if res > self.rank:
            raise ValueError("axis {0:d} out of rank {1:d}".format(res, self.rank))
        elif res < 0:
            res += self.rank
        return res

    def get_leg_indices(self, labels):
        """Translate a list of leg-indices or leg-labels to leg indices.

        Parameters
        ----------
        labels : iterable of string/int
            The leg-labels (or directly indices) to be translated in leg-indices

        Returns
        -------
        leg_indices : list of int
            the translated labels.

        See also
        --------
        get_leg_index : used to translate each of the single entries.
        set_leg_labels : set the labels of different legs.
        """
        return [self.get_leg_index(l) for l in labels]

    def set_leg_labels(self, labels):
        """Return labels for the legs.

        Introduction to leg labeling can be found in :doc:`../IntroNpc`.

        Parameters
        ----------
        labels : iterable (strings | None), len=self.rank
            One label for each of the legs.
            An entry can be None for an anonymous leg.

        See also
        --------
        get_leg: translate the labels to indices
        get_legs: calls get_legs for an iterable of labels
        """
        if len(labels) != self.rank:
            raise ValueError("Need one leg label for each of the legs.")
        self.labels = {}
        for i, l in enumerate(labels):
            if l == '':
                raise ValueError("use `None` for empty labels")
            if l is not None:
                self.labels[l] = i

    def get_leg_labels(self):
        """Return tuple of the leg labels, with `None` for anonymous legs."""
        lb = [None] * self.rank
        for k, v in self.labels.iteritems():
            lb[v] = k
        return tuple(lb)

    # string output ===========================================================

    def __repr__(self):
        return "<npc.array shape={0!s} charge={1!s} labels={2!s}>".format(self.shape, self.chinfo,
                                                                          self.get_leg_labels())

    def __str__(self):
        res = "\n".join([repr(self)[:-1], str(self.to_ndarray()), ">"])
        return res

    def sparse_stats(self):
        """Returns a string detailing the sparse statistics"""
        total = np.prod(self.shape)
        if total is 0:
            return "Array without entries, one axis is empty."
        nblocks = self.stored_blocks
        stored = self.size
        nonzero = np.sum([np.count_nonzero(t) for t in self._data], dtype=np.int_)
        bs = np.array([t.size for t in self._data], dtype=np.float)
        if nblocks > 0:
            bs1 = (np.sum(bs**0.5) / nblocks)**2
            bs2 = np.sum(bs) / nblocks
            bs3 = (np.sum(bs**2.0) / nblocks)**0.5
            captsparse = float(nonzero) / stored
        else:
            captsparse = 1.
            bs1, bs2, bs3 = 0, 0, 0
        res = "{nonzero:d} of {total:d} entries (={nztotal:g}) nonzero,\n" \
            "stored in {nblocks:d} blocks with {stored:d} entries.\n" \
            "Captured sparsity: {captsparse:g}\n"  \
            "Effective block sizes (second entry=mean): [{bs1:.2f}, {bs2:.2f}, {bs3:.2f}]"

        return res.format(
            nonzero=nonzero,
            total=total,
            nztotal=nonzero / total,
            nblocks=nblocks,
            stored=stored,
            captsparse=captsparse,
            bs1=bs1,
            bs2=bs2,
            bs3=bs3)

    # accessing entries =======================================================

    def to_ndarray(self):
        """convert self to a dense numpy ndarray."""
        res = np.zeros(self.shape, self.dtype)
        for block, slices, _, _ in self:  # that's elegant! :)
            res[slices] = block
        return res

    def __iter__(self):
        """Allow to iterate over the non-zero blocks, giving all `_data`.

        Yields
        ------
        block : ndarray
            the actual entries of a charge block
        blockslices : tuple of slices
            a slice giving the range of the block in the original tensor for each of the legs
        charges : list of charges
            the charge value(s) for each of the legs (takink `qconj` into account)
        qdat : ndarray
            the qindex for each of the legs
        """
        for block, qdat in itertools.izip(self._data, self._qdata):
            blockslices = []
            qs = []
            for (qi, l) in itertools.izip(qdat, self.legs):
                blockslices.append(l.get_slice(qi))
                qs.append(l.get_charge(qi))
            yield block, tuple(blockslices), qs, qdat

    def __getitem__(self, inds):
        """acces entries with ``self[inds]``

        Parameters
        ----------
        inds : tuple
            A tuple specifying the `index` for each leg.
            An ``Ellipsis`` (written as ``...``) replaces ``slice(None)`` for missing axes.
            For a single `index`, we currently support:

            - A single integer, choosing an index of the axis,
              reducing the dimension of the resulting array.
            - A ``slice(None)`` specifying the complete axis.
            - A ``slice``, which acts like a `mask` in :meth:`iproject`.
            - A 1D array_like(bool): acts like a `mask` in :meth:`iproject`.
            - A 1D array_like(int): acts like a `mask` in :meth:`iproject`,
              and if not orderd, a subsequent permuation with :meth:`permute`

        Returns
        -------
        res : `dtype`
            only returned, if a single integer is given for all legs.
            It is the entry specified by `inds`, giving ``0.`` for non-saved blocks.
        or
        sliced : :class:`Array`
            a copy with some of the data removed by :meth:`take_slice` and/or :meth:`project`

        Notes
        -----
        ``self[i]`` is equivalent to ``self[i, ...]``.
        ``self[i, ..., j]`` is syntactic sugar for ``self[(i, Ellipsis, i2)]``

        Raises
        ------
        IndexError
            If the number of indices is too large, or
            if an index is out of range.
        """
        int_only, inds = self._pre_indexing(inds)
        if int_only:
            pos = np.array([l.get_qindex(i) for i, l in zip(inds, self.legs)])
            block = self._get_block(pos[:, 0])
            if block is None:
                return self.dtype.type(0)
            else:
                return block[tuple(pos[:, 1])]
        # advanced indexing
        return self._advanced_getitem(inds)

    def __setitem__(self, inds, other):
        """assign ``self[inds] = other``.

        Should work as expected for both basic and advanced indexing as described in
        :meth:`__getitem__`.
        `other` can be:
        - a single value (if all of `inds` are integer)
        or for slicing/advanced indexing:
        - a :class:`Array`, with charges as ``self[inds]`` returned by :meth:`__getitem__`.
        - or a flat numpy array, assuming the charges as with ``self[inds]``.
        """
        int_only, inds = self._pre_indexing(inds)
        if int_only:
            pos = np.array([l.get_qindex(i) for i, l in zip(inds, self.legs)])
            block = self._get_block(pos[:, 0], insert=True, raise_incomp_q=True)
            block[tuple(pos[:, 1])] = other
            return
        # advanced indexing
        if not isinstance(other, Array):
            # if other is a flat array, convert it to an npc Array
            like_other = self.zeros_like()._advanced_getitem(inds)
            other = Array.from_ndarray(other, self.chinfo, like_other.legs, self.dtype,
                                       like_other.qtotal)
        self._advanced_setitem_npc(inds, other)

    def take_slice(self, indices, axes):
        """Return a copy of self fixing `indices` along one or multiple `axes`.

        For a rank-4 Array ``A.take_slice([i, j], [1,2])`` is equivalent to ``A[:, i, j, :]``.

        Parameters
        ----------
        indices : (iterable of) int
            the (flat) index for each of the legs specified by `axes`
        axes : (iterable of) str/int
            leg labels or indices to specify the legs for which the indices are given.

        Returns
        -------
        slided_self : :class:`Array`
            a copy of self, equivalent to taking slices with indices inserted in axes.
        """
        axes = self.get_leg_indices(toiterable(axes))
        indices = np.asarray(toiterable(indices), dtype=np.intp)
        if len(axes) != len(indices):
            raise ValueError("len(axes) != len(indices)")
        if indices.ndim != 1:
            raise ValueError("indices may only contain ints")
        res = self.copy(deep=True)
        if len(axes) == 0:
            return res  # nothing to do
        # qindex and index_within_block for each of the axes
        pos = np.array([self.legs[a].get_qindex(i) for a, i in zip(axes, indices)])
        # which axes to keep
        keep_axes = [a for a in xrange(self.rank) if a not in axes]
        res.legs = [self.legs[a] for a in keep_axes]
        res._set_shape()
        labels = self.get_leg_labels()
        res.set_leg_labels([labels[a] for a in keep_axes])
        # calculate new total charge
        for a, (qi, _) in zip(axes, pos):
            res.qtotal -= self.legs[a].get_charge(qi)
        res.qtotal = self.chinfo.make_valid(res.qtotal)
        # which blocks to keep
        axes = np.array(axes, dtype=np.intp)
        keep_axes = np.array(keep_axes, dtype=np.intp)
        keep_blocks = np.all(self._qdata[:, axes] == pos[:, 0], axis=1)
        res._qdata = self._qdata[np.ix_(keep_blocks, keep_axes)]
        # res._qdata_sorted is not changed
        # determine the slices to take on _data
        sl = [slice(None)] * self.rank
        for a, ri in zip(axes, pos[:, 1]):
            sl[a] = ri  # the indices within the blocks
        sl = tuple(sl)
        # finally take slices on _data
        res._data = [block[sl] for block, k in itertools.izip(res._data, keep_blocks) if k]
        return res

    # handling of charges =====================================================

    def detect_ndarray_qtotal(self, flat_array, cutoff=None):
        """ Returns the total charge of first non-zero sector found in `a`.

        Charge information is taken from self.
        If you have only the charge data, create an empty Array(chinf, legcharges).

        Parameters
        ----------
        flat_array : array
            the flat numpy array from which you want to detect the charges
        chinfo : ChargeInfo
            the nature of the charge
        legcharges : list of LegCharge
            for each leg the LegCharge
        cutoff : float
            Blocks with ``np.max(np.abs(block)) > cutoff`` are considered as zero.
            Defaults to :data:`QCUTOFF`.

        Returns
        -------
        qtotal : charge
            the total charge fo the first non-zero (i.e. > cutoff) charge block
        """
        if cutoff is None:
            cutoff = QCUTOFF
        for qindices in self._iter_all_blocks():
            sl = self._get_block_slices(qindices)
            if np.any(np.abs(flat_array[sl]) > cutoff):
                return self._get_block_charge(qindices)
        warnings.warn("can't detect total charge: no entry larger than cutoff. Return 0 charge.")
        return self.chinfo.make_valid()

    def gauge_total_charge(self, leg, newqtotal=None):
        """changes the total charge of an Array `A` inplace by adjusting the charge on a certain leg.

        The total charge is given by finding a nonzero entry [i1, i2, ...] and calculating::

            qtotal = sum([l.qind[qi, 2:] * l.conj for i, l in zip([i1,i2,...], self.legs)])

        Thus, the total charge can be changed by redefining the leg charge of a given leg.
        This is exaclty what this function does.

        Parameters
        ----------
        leg : int or string
            the new leg (index or label), for which the charge is changed
        newqtotal : charge values, defaults to 0
            the new total charge
        """
        leg = self.get_leg_index(leg)
        newqtotal = self.chinfo.make_valid(newqtotal)  # converts to array, default zero
        chdiff = newqtotal - self.qtotal
        if isinstance(leg, LegPipe):
            raise ValueError("not possible for a LegPipe. Convert to a LegCharge first!")
        newleg = copy_.copy(self.legs[leg])  # shallow copy of the LegCharge
        newleg.qind = newleg.qind.copy()
        newleg.qind[:, 2:] = self.chinfo.make_valid(newleg.qind[:, 2:] + newleg.qconj * chdiff)
        self.legs[leg] = newleg
        self.qtotal = newqtotal

    def is_completely_blocked(self):
        """returns bool wheter all legs are blocked by charge"""
        return all([l.is_blocked() for l in self.legs])

    def sort_legcharge(self, sort=True, bunch=True):
        """Return a copy with one ore all legs sorted by charges.

        Sort/bunch one or multiple of the LegCharges.
        Legs which are sorted *and* bunched are guaranteed to be blocked by charge.

        Parameters
        ----------
        sort : True | False | list of {True, False, perm}
            A single bool holds for all legs, default=True.
            Else, `sort` should contain one entry for each leg, with a bool for sort/don't sort,
            or a 1D array perm for a given permuation to apply to a leg.
        bunch : True | False | list of {True, False}
            A single bool holds for all legs, default=True.
            whether or not to bunch at each leg, i.e. combine contiguous blocks with equal charges.

        Returns
        -------
        perm : tuple of 1D arrays
            the permutation applied to each of the legs.
            cp.to_ndarray() = self.to_ndarray(perm)
        result : Array
            a shallow copy of self, with legs sorted/bunched
        """
        if sort is False or sort is True:  # ``sort in [False, True]`` doesn't work...
            sort = [sort] * self.rank
        if bunch is False or bunch is True:
            bunch = [bunch] * self.rank
        if not len(sort) == len(bunch) == self.rank:
            raise ValueError("Wrong len for bunch or sort")
        cp = self.copy(deep=False)
        cp._qdata = cp._qdata.copy()  # Array views may share _qdata, so make a copy first
        for li in xrange(self.rank):
            if sort[li] is not False:
                if sort[li] is True:
                    if cp.legs[li].sorted:  # optimization if the leg is sorted already
                        sort[li] = np.arange(cp.shape[li])
                        continue
                    p_qind, newleg = cp.legs[li].sort(bunch=False)
                    sort[li] = cp.legs[li].perm_flat_from_perm_qind(
                        p_qind)  # called for the old leg
                    cp.legs[li] = newleg
                else:
                    try:
                        p_qind = self.legs[li].perm_qind_from_perm_flat(sort[li])
                    except ValueError:  # permutation mixes qindices
                        cp = cp.permute(sort[li], axes=[li])
                        continue
                cp._perm_qind(p_qind, li)
            else:
                sort[li] = np.arange(cp.shape[li])
        if any(bunch):
            cp = cp._bunch(bunch)  # bunch does not permute...
        return tuple(sort), cp

    def isort_qdata(self):
        """(lex)sort ``self._qdata``. In place.

        Lexsort ``self._qdata`` and ``self._data`` and set ``self._qdata_sorted = True``.
        """
        if self._qdata_sorted:
            return
        if len(self._qdata) < 2:
            self._qdata_sorted = True
            return
        perm = np.lexsort(self._qdata.T)
        self._qdata = self._qdata[perm, :]
        self._data = [self._data[p] for p in perm]
        self._qdata_sorted = True

    # reshaping ===============================================================

    def make_pipe(self, axes, **kwargs):
        """generates a :class:`~tenpy.linalg.charges.LegPipe` for specified axes.

        Parameters
        ----------
        axes : iterable of str|int
            the leg labels for the axes which should be combined. Order matters!
        **kwargs :
            additional keyword arguments given to :class:`~tenpy.linalg.charges.LegPipe`

        Returns
        -------
        pipe : :class:`~tenpy.linalg.charges.LegPipe`
            A pipe of the legs specified by axes.
        """
        axes = self.get_leg_indices(axes)
        legs = [self.legs[a] for a in axes]
        return charges.LegPipe(legs, **kwargs)

    def combine_legs(self, combine_legs, new_axes=None, pipes=None, qconj=None):
        """Reshape: combine multiple legs into multiple pipes. If necessary, transpose before.

        Parameters
        ----------
        combine_legs : (iterable of) iterable of {str|int}
            bundles of leg indices or labels, which should be combined into a new output pipes.
            If multiple pipes should be created, use a list fore each new pipe.
        new_axes : None | (iterable of) int
            The leg-indices, at which the combined legs should appear in the resulting array.
            Default: for each pipe the position of its first pipe in the original array,
            (taking into account that some axes are 'removed' by combining).
            Thus no transposition is perfomed if `combine_legs` contains only contiguous ranges.
        pipes : None | (iterable of) {:class:`LegPipes` | None}
            optional: provide one or multiple of the resulting LegPipes to avoid overhead of
            computing new leg pipes for the same legs multiple times.
            The LegPipes are conjugated, if that is necessary for compatibility with the legs.
        qconj : (iterable of) {+1, -1}
            specify whether new created pipes point inward or outward. Defaults to +1.
            Ignored for given `pipes`, which are not newly calculated.

        Returns
        -------
        reshaped : :class:`Array`
            A copy of self, whith some legs combined into pipes as specified by the arguments.

        Notes
        -----
        Labels are inherited from self.
        New pipe labels are generated as ``'(' + '.'.join(*leglabels) + ')'``.
        For these new labels, previously unlabeled legs are replaced by ``'?#'``,
        where ``#`` is the leg-index in the original tensor `self`.

        Examples
        --------
        >>> oldarray.set_leg_labels(['a', 'b', 'c', 'd', 'e'])
        >>> c1 = oldarray.combine_legs([1, 2], qconj=-1)  # only single output pipe
        >>> c1.get_leg_labels()
        ['a', '(b.c)', 'd', 'e']

        Indices of `combine_legs` refer to the original array.
        If transposing is necessary, it is performed automatically:

        >>> c2 = oldarray.combine_legs([[0, 3], [4, 1]], qconj=[+1, -1]) # two output pipes
        >>> c2.get_leg_labels()
        ['(a.d)', 'c', '(e.b)']
        >>> c3 = oldarray.combine_legs([['a', 'd'], ['e', 'b']], new_axes=[2, 1],
        >>>                            pipes=[c2.legs[0], c2.legs[2]])
        >>> c3.get_leg_labels()
        ['b', '(e.b)', '(a.d)']
        """
        # bring arguments into a standard form
        combine_legs = list(combine_legs)  # convert iterable to list
        # check: is combine_legs `iterable(iterable(int|str))` or `iterable(int|str)` ?
        if [combine_legs[0]] == toiterable(combine_legs[0]):
            # the first entry is (int|str) -> only a single new pipe
            combine_legs = [combine_legs]
            if new_axes is not None:
                new_axes = toiterable(new_axes)
            if pipes is not None:
                pipes = toiterable(pipes)
        pipes = self._combine_legs_make_pipes(combine_legs, pipes, qconj)  # out-sourced
        # good for index tricks: convert combine_legs into arrays
        combine_legs = [np.asarray(self.get_leg_indices(cl), dtype=np.intp) for cl in combine_legs]
        all_combine_legs = np.concatenate(combine_legs)
        if len(set(all_combine_legs)) != len(all_combine_legs):
            raise ValueError("got a leg multiple times: " + str(combine_legs))
        new_axes, transp = self._combine_legs_new_axes(combine_legs, new_axes)  # out-sourced
        # permute arguments sucht that new_axes is sorted ascending
        perm_args = np.argsort(new_axes)
        combine_legs = [combine_legs[p] for p in perm_args]
        pipes = [pipes[p] for p in perm_args]
        new_axes = [new_axes[p] for p in perm_args]

        # labels: replace non-set labels with '?#' (*before* transpose
        labels = [(l if l is not None else '?' + str(i))
                  for i, l in enumerate(self.get_leg_labels())]

        # transpose if necessary
        if transp != tuple(range(self.rank)):
            res = self.copy(deep=False)
            res.set_leg_labels(labels)
            res = res.transpose(transp)
            tr_combine_legs = [range(na, na + len(cl)) for na, cl in zip(new_axes, combine_legs)]
            return res.combine_legs(tr_combine_legs, new_axes=new_axes, pipes=pipes)
        # if we come here, combine_legs has the form of `tr_combine_legs`.

        # the **main work** of copying the data is sourced out, now that we have the
        # standard form of our arguments
        res = self._combine_legs_worker(combine_legs, new_axes, pipes)

        # get new labels
        pipe_labels = [('(' + '.'.join([labels[c] for c in cl]) + ')') for cl in combine_legs]
        for na, p, plab in zip(new_axes, pipes, pipe_labels):
            labels[na:na + p.nlegs] = [plab]
        res.set_leg_labels(labels)
        return res

    def split_legs(self, axes=None, cutoff=0.):
        """Reshape: opposite of combine_legs: split (some) legs which are LegPipes.

        Parameters
        ----------
        axes : (iterable of) int|str
            leg labels or indices determining the axes to split.
            The corresponding entries in self.legs must be :class:`LegPipe` instances.
            Defaults to all legs, which are :class:`LegPipe` instances.
        cutoff : float
            Splitted data blocks with ``np.max(np.abs(block)) > cutoff`` are considered as zero.
            Defaults to 0.

        Returns
        -------
        reshaped : :class:`Array`
            a copy of self where the specified legs are splitted.

        Notes
        -----
        Labels are split reverting what was done in :meth:`combine_legs`.
        '?#' labels are replaced with ``None``.
        """
        if axes is None:
            axes = [i for i, l in enumerate(self.legs) if isinstance(l, LegPipe)]
        else:
            axes = self.get_leg_indices(toiterable(axes))
            if len(set(axes)) == len(axes):
                raise ValueError("can't split a leg multiple times!")
        for ax in axes:
            if not isinstance(self.legs[ax], LegPipe):
                raise ValueError("can't split leg {ax:d} which is not a LegPipe".format(ax=ax))
        if len(axes) == 0:
            return self.copy(deep=True)

        res = self._split_legs_worker(axes, cutoff)

        labels = list(self.get_leg_labels())
        for a in axes:
            labels[a:a + 1] = self._split_leg_label(labels[a], self.legs[a].nlegs)
        res.set_leg_labels(labels)
        return res

    def squeeze(self, axes=None):
        """Like ``np.squeeze``.

        If a squeezed leg has non-zero charge, this charge is added to :attr:`qtotal`.

        Parameters
        ----------
        axes : None | (iterable of) {int|str}
            labels or indices of the legs which should be 'squeezed', i.e. the legs removed.
            The corresponding legs must be trivial, i.e., have `ind_len` 1.

        Returns
        -------
        squeezed : :class:Array | scalar
            A scalar of ``self.dtype``, if all axes were squeezed.
            Else a copy of ``self`` with reduced ``rank`` as specified by `axes`.
        """
        if axes is None:
            axes = tuple([a for a in range(self.rank) if self.shape[a] == 1])
        else:
            axes = tuple(self.get_leg_indices(toiterable(axes)))
        for a in axes:
            if self.shape[a] != 1:
                raise ValueError("Tried to squeeze non-unit leg")
        keep = [a for a in range(self.rank) if a not in axes]
        if len(keep) == 0:
            index = tuple([0] * self.rank)
            return self[index]
        res = self.copy(deep=False)
        # adjust qtotal
        res.legs = tuple([self.legs[a] for a in keep])
        res._set_shape()
        for a in axes:
            res.qtotal -= self.legs[a].get_charge(0)
        res.qtotal = self.chinfo.make_valid(res.qtotal)

        labels = self.get_leg_labels()
        res.set_leg_labels([labels[a] for a in keep])

        res._data = [np.squeeze(t, axis=axes).copy() for t in self._data]
        res._qdata = self._qdata[:, np.array(keep)]
        # res._qdata_sorted doesn't change
        return res

    # data manipulation =======================================================

    def astype(self, dtype):
        """Return (deep) copy with new dtype, upcasting all blocks in ``_data``.

        Parameters
        ----------
        dtype : convertible to a np.dtype
            the new data type.
            If None, deduce the new dtype as common type of ``self._data``.

        Returns
        -------
        copy : :class:`Array`
            deep copy of self with new dtype
        """
        cp = self.copy(deep=False)  # manual deep copy: don't copy every block twice
        cp._qdata = cp._qdata.copy()
        if dtype is None:
            dtype = np.common_dtype(*self._data)
        cp.dtype = np.dtype(dtype)
        cp._data = [d.astype(self.dtype, copy=True) for d in self._data]
        return cp

    def imake_contiguous(self):
        """make each of the blocks contigous with `np.ascontigousarray`.

        Might speed up subsequent tensordot & co, if the blocks were not contiguous before."""
        self._data = [np.ascontigousarray(t) for t in self.dat]
        return self

    def ipurge_zeros(self, cutoff=QCUTOFF, norm_order=None):
        """Removes ``self._data`` blocks with *norm* less than cutoff. In place.

        Parameters
        ----------
        cutoff : float
            blocks with norm <= `cutoff` are removed. defaults to :data:`QCUTOFF`.
        norm_order :
            a valid `ord` argument for `np.linalg.norm`.
            Default ``None`` gives the Frobenius norm/2-norm for matrices/everything else.
            Note that this differs from other methods, e.g. :meth:`from_ndarray`,
            which use the maximum norm.
        """
        if len(self._data) == 0:
            return self
        norm = np.array([np.linalg.norm(t, ord=norm_order) for t in self._data])
        keep = (norm > cutoff)  # bool array
        self._data = [t for t, k in itertools.izip(self._data, keep) if k]
        self._qdata = self._qdata[keep]
        # self._qdata_sorted is preserved
        return self

    def iproject(self, mask, axes):
        """Applying masks to one or multiple axes. In place.

        This function is similar as `np.compress` with boolean arrays
        For each specified axis, a boolean 1D array `mask` can be given,
        which chooses the indices to keep.

        .. warning ::
            Although it is possible to use an 1D int array as a mask, the order is ignored!
            If you need to permute an axis, use :meth:`permute` or :meth:`sort_legcharge`.

        Parameters
        ----------
        mask : (list of) 1D array(bool|int)
            for each axis specified by `axes` a mask, which indices of the axes should be kept.
            If `mask` is a bool array, keep the indices where `mask` is True.
            If `mask` is an int array, keep the indices listed in the mask, *ignoring* the
            order or multiplicity.
        axes : (list of) int | string
            The `i`th entry in this list specifies the axis for the `i`th entry of `mask`,
            either as an int, or with a leg label.
            If axes is just a single int/string, specify just one mask.

        Returns
        -------
        map_qind : list of 1D arrays
            the mapping of qindices for each of the specified axes.
        block_masks: list of lists of 1D bool arrays
            ``block_masks[a][qind]`` is a boolen mask which indices to keep
            in block ``qindex`` of ``axes[a]``
        """
        axes = self.get_leg_indices(toiterable(axes))
        mask = [np.asarray(m) for m in toiterable(mask)]
        if len(axes) != len(mask):
            raise ValueError("len(axes) != len(mask)")
        if len(axes) == 0:
            return [], []  # nothing to do.
        for i, m in enumerate(mask):
            # convert integer masks to bool masks
            if m.dtype != np.bool_:
                mask[i] = np.zeros(self.shape[axes[i]], dtype=np.bool_)
                np.put(mask[i], m, True)
        # Array views may share ``_qdata`` views, so make a copy of _qdata before manipulating
        self._qdata = self._qdata.copy()
        block_masks = []
        proj_data = np.arange(self.stored_blocks)
        map_qind = []
        for m, a in zip(mask, axes):
            l = self.legs[a]
            m_qind, bm, self.legs[a] = l.project(m)
            map_qind.append(m_qind)
            block_masks.append(bm)
            q = self._qdata[:, a] = m_qind[self._qdata[:, a]]
            piv = (q >= 0)
            self._qdata = self._qdata[piv]  # keeps dimension
            # self._qdata_sorted is preserved
            proj_data = proj_data[piv]
        self._set_shape()
        # finally project out the blocks
        data = []
        for i, iold in enumerate(proj_data):
            block = self._data[iold]
            subidx = [slice(d) for d in block.shape]
            for m, a in zip(block_masks, axes):
                subidx[a] = m[self._qdata[i, a]]
                block = np.compress(m[self._qdata[i, a]], block, axis=a)
            data.append(block)
        self._data = data
        return map_qind, block_masks

    def permute(self, perm, axis):
        """Apply a permutation in the indices of an axis.

        Similar as np.take with a 1D array.
        Roughly equivalent to ``res[:, ...] = self[perm, ...]`` for the corresponding `axis`.
        .. warning ::

            This function is quite slow, and usually not needed during core algorithms.

        Parameters
        ----------
        perm : array_like 1D int
            The permutation which should be applied to the leg given by `axis`
        axis : str | int
            a leg label or index specifying on which leg to take the permutation.

        Returns
        -------
        res : :class:`Array`
            a copy of self with leg `axis` permuted, such that
            ``res[i, ...] = self[perm[i], ...]`` for ``i`` along `axis`

        See also
        --------
        sort_legcharge : can also be used to perform a general permutation.
            However, it is faster for permutations which don't mix blocks.
        """
        axis = self.get_leg_index(axis)
        perm = np.asarray(perm, dtype=np.intp)
        oldleg = self.legs[axis]
        if len(perm) != oldleg.ind_len:
            raise ValueError("permutation has wrong length")
        rev_perm = reverse_sort_perm(perm)
        newleg = LegCharge.from_qflat(self.chinfo, oldleg.to_qflat()[perm], oldleg.qconj)
        newleg = newleg.bunch()[1]
        res = self.copy(deep=False)  # data is replaced afterwards
        res.legs[axis] = newleg
        qdata_axis = self._qdata[:, axis]
        new_block_idx = [slice(None)] * self.rank
        old_block_idx = [slice(None)] * self.rank
        data = []
        qdata = {}  # dict for fast look up: tuple(indices) -> _data index
        for old_qind, old_qind_row in enumerate(oldleg.qind):
            old_range = xrange(old_qind_row[0], old_qind_row[1])
            for old_data_index in np.nonzero(qdata_axis == old_qind)[0]:
                old_block = self._data[old_data_index]
                old_qindices = self._qdata[old_data_index]
                new_qindices = old_qindices.copy()
                for i_old in old_range:
                    i_new = rev_perm[i_old]
                    qi_new, within_new = newleg.get_qindex(i_new)
                    new_qindices[axis] = qi_new
                    # look up new_qindices in `qdata`, insert them if necessary
                    new_data_ind = qdata.setdefault(tuple(new_qindices), len(data))
                    if new_data_ind == len(data):
                        # insert new block
                        data.append(np.zeros(res._get_block_shape(new_qindices)))
                    new_block = data[new_data_ind]
                    # copy data
                    new_block_idx[axis] = within_new
                    old_block_idx[axis] = i_old - old_qind_row[0]
                    new_block[tuple(new_block_idx)] = old_block[tuple(old_block_idx)]
        # data blocks copied
        res._data = data
        res._qdata_sorted = False
        res_qdata = res._qdata = np.empty((len(data), self.rank), dtype=np.intp)
        for qindices, i in qdata.iteritems():
            res_qdata[i] = qindices
        return res

    def itranspose(self, axes=None):
        """Transpose axes like `np.transpose`. In place.

        Parameters
        ----------
        axes: iterable (int|string), len ``rank`` | None
            the new order of the axes. By default (None), reverse axes.
        """
        if axes is None:
            axes = tuple(reversed(xrange(self.rank)))
        else:
            axes = tuple(self.get_leg_indices(axes))
            if len(axes) != self.rank or len(set(axes)) != self.rank:
                raise ValueError("axes has wrong length: " + str(axes))
            if axes == tuple(xrange(self.rank)):
                return self  # nothing to do
        axes_arr = np.array(axes)
        self.legs = [self.legs[a] for a in axes]
        self._set_shape()
        labs = self.get_leg_labels()
        self.set_leg_labels([labs[a] for a in axes])
        self._qdata = self._qdata[:, axes_arr]
        self._qdata_sorted = False
        self._data = [np.transpose(block, axes) for block in self._data]
        return self

    def transpose(self, axes=None):
        """Like :meth:`itranspose`, but on a deep copy."""
        cp = self.copy(deep=True)
        cp.itranspose(axes)
        return cp

    def iswapaxes(self, axis1, axis2):
        """similar as ``np.swapaxes``. In place."""
        axis1 = self.get_leg_index(axis1)
        axis2 = self.get_leg_index(axis2)
        if axis1 == axis2:
            return self  # nothing to do
        swap = np.arange(self.rank, dtype=np.intp)
        swap[axis1], swap[axis2] = axis2, axis1
        legs = self.legs
        legs[axis1], legs[axis2] = legs[axis2], legs[axis1]
        for k, v in self.labels.iteritems():
            if v == axis1:
                self.labels[k] = axis2
            if v == axis2:
                self.labels[k] = axis1
        self._set_shape()
        self._qdata = self._qdata[:, swap]
        self._qdata_sorted = False
        self._data = [t.swapaxes(axis1, axis2) for t in self._data]
        return self

    def iscale_axis(self, s, axis=-1):
        """scale with varying values along an axis. In place.

        Rescale to ``new_self[i1, ..., i_axis, ...] = s[i_axis] * self[i1, ..., i_axis, ...]``.

        Parameters
        ----------
        s : 1D array, len=self.shape[axis]
            the vector with which the axis should be scaled
        axis : str|int
            the leg label or index for the axis which should be scaled.

        See also
        --------
        iproject : can be used to discard indices for which s is zero.
        """
        axis = self.get_leg_index(axis)
        s = np.asarray(s)
        if s.shape != (self.shape[axis], ):
            raise ValueError("s has wrong shape: ", str(s.shape))
        self.dtype = np.find_common_type([self.dtype], [s.dtype])
        leg = self.legs[axis]
        if axis != self.rank - 1:
            self._data = [np.swapaxes(np.swapaxes(t, axis, -1) * s[leg.get_slice(qi)], axis, -1)
                          for qi, t in itertools.izip(self._qdata[:, axis], self._data)]
        else:  # optimize: no need to swap axes, if axis is -1.
            self._data = [t * s[leg.get_slice(qi)]  # (it's slightly faster for large arrays)
                          for qi, t in itertools.izip(self._qdata[:, axis], self._data)]
        return self

    def scale_axis(self, s, axis=-1):
        """Samse as :meth:`iscale_axis`, but return a (deep) copy."""
        res = self.copy(deep=False)
        res._qdata = res._qdata.copy()
        res.iscale_axis(s, axis)
        return res

    # block-wise operations == element wise with numpy ufunc

    def iunary_blockwise(self, func, *args, **kwargs):
        """Roughly ``self = f(self)``, block-wise. In place.

        Applies an unary function `func` to the non-zero blocks in ``self._data``.

        .. note ::
            Assumes implicitly that ``func(np.zeros(...), *args, **kwargs)`` gives 0,
            since we don't let `func` act on zero blocks!

        Parameters
        ----------
        func : function
            A function acting on flat arrays, returning flat arrays.
            It is called like ``new_block = func(block, *args, **kwargs)``.
        *args :
            additional arguments given to function *after* the block
        **kwargs :
            keyword arguments given to the function

        Examples
        --------
        >>> a.iunaray_blockwise(np.real)  # get real part
        >>> a.iunaray_blockwise(np.conj)  # same data as a.iconj(), but doesn't charge conjugate.
        """
        self._data = [func(t, *args, **kwargs) for t in self._data]
        if len(self._data) > 0:
            self.dtype = self._data[0].dtype
        return self

    def unary_blockwise(self, func, *args, **kwargs):
        """Roughly ``return func(self)``, block-wise. Copies.

        Same as :meth:`iunary_blockwise`, but makes a **shallow** copy first."""
        res = self.copy(deep=False)
        return res.iunary_blockwise(func, *args, **kwargs)

    def iconj(self, complex_conj=True):
        """wraper around :meth:`self.conj` with ``inplace=True``"""
        return self.conj(complex_conj, inplace=True)

    def conj(self, complex_conj=True, inplace=False):
        """conjugate: complex conjugate data, conjugate charge data.

        Conjugate all legs, set negative qtotal.

        Labeling: takes 'a' -> 'a*', 'a*'-> 'a' and
        '(a,(b*,c))' -> '(a*, (b, c*))'

        Parameters
        ----------
        complex_conj : bool
            Wheter the data should be complex conjugated.
        inplace : bool
            wheter to apply changes to `self`, or to return a *deep* copy
        """
        if self.dtype.kind == 'c' and complex_conj:
            if inplace:
                res = self.iunary_blockwise(np.conj)
            else:
                res = self.unary_blockwise(np.conj)
        else:
            if inplace:
                res = self
            else:
                res = self.copy(deep=True)
        res.qtotal = -res.qtotal
        res.legs = [l.conj() for l in res.legs]
        labels = {}
        for lab, ax in res.labels.iteritems():
            labels[self._conj_leg_label(lab)] = ax
        res.labels = labels
        return res

    def norm(self, ord=None, convert_to_float=True):
        """Norm of flattened data.

        See :func:`norm` for details."""
        if ord == 0:
            return np.sum([np.count_nonzero(t) for t in self._data], dtype=np.int_)
        if convert_to_float:
            new_type = np.find_common_type([np.float_, self.dtype], [])  # int -> float
            if new_type != self.dtype:
                return self.astype(new_type).norm(ord, False)
        block_norms = [np.linalg.norm(t.reshape(-1), ord) for t in self._data]
        # ``.reshape(-1) gives a 1D view and is thus faster than ``.flatten()``
        # add a [0] in the list to ensure correct results for ``ord=-inf``
        return np.linalg.norm(block_norms + [0], ord)

    def __neg__(self):
        """return ``-self``"""
        return self.unary_blockwise(np.negative)

    def ibinary_blockwise(self, func, other, *args, **kwargs):
        """Roughly ``self = func(self, other)``, block-wise. In place.

        Applies a binary function 'block-wise' to the non-zero blocks of
        ``self._data`` and ``other._data``, storing result in place.
        Assumes that `other` is an :class:`Array` as well, with the same shape
        and compatible legs.

        .. note ::
            Assumes implicitly that
            ``func(np.zeros(...), np.zeros(...), *args, **kwargs)`` gives 0,
            since we don't let `func` act on zero blocks!

        Examples
        --------
        >>> a.ibinary_blockwise(np.add, b)  # equivalent to ``a += b``, if ``b`` is an `Array`.
        >>> a.ibinary_blockwise(np.max, b)  # overwrites ``a`` to ``a = max(a, b)``
        """
        for self_leg, other_leg in zip(self.legs, other.legs):
            self_leg.test_equal(other_leg)
        self.isort_qdata()
        other.isort_qdata()

        adata = self._data
        bdata = other._data
        aq = self._qdata
        bq = other._qdata
        Na, Nb = len(aq), len(bq)

        # If the q_dat structure is identical, we can immediately run through the data.
        if Na == Nb and np.array_equiv(aq, bq):
            self._data = [func(at, bt, *args, **kwargs) for at, bt in itertools.izip(adata, bdata)]
        else:  # have to step through comparing left and right qdata
            i, j = 0, 0
            qdata = []
            data = []
            while i < Na or j < Nb:
                if tuple(aq[i]) == tuple(bq[j]):  # a and b are non-zero
                    data.append(func(adata[i], bdata[j], *args, **kwargs))
                    qdata.append(aq[i])
                    i += 1
                    j += 1
                elif j >= Nb or (tuple(aq[i, ::-1]) < tuple(bq[j, ::-1])):  # b is 0
                    data.append(func(adata[i], np.zeros_like(adata[i]), *args, **kwargs))
                    qdata.append(aq[i])
                    i += 1
                else:  # a is 0
                    data.append(func(np.zeros_like(bdata[j]), bdata[j], *args, **kwargs))
                    qdata.append(bq[j])
                    j += 1
                # if both are zero, we assume f(0, 0) = 0
            self._data = data
            self._qdata = np.array(qdata, dtype=np.intp).reshape((len(data), self.rank))
            # ``self._qdata_sorted = True`` was set by self.isort_qdata
        if len(self._data) > 0:
            self.dtype = self._data[0].dtype
        return self

    def binary_blockwise(self, func, other, *args, **kwargs):
        """Roughly ``return func(self, other)``, block-wise. Copies.

        Same as :meth:`ibinary_blockwise`, but makes a **shallow** copy first.
        """
        res = self.copy(deep=False)
        return res.ibinary_blockwise(func, other, *args, **kwargs)

    def matvec(self, other):
        """This function is used by the Lanczos algorithm needed for DMRG.

        It is supposed to calculate the matrix - vector - product
        for a rank-2 matrix ``self`` and a rank-1 vector `other`.
        """
        return tensordot(self, other, axes=1)

    def __add__(self, other):
        """return self + other"""
        if isinstance(other, Array):
            return self.binary_blockwise(np.add, other)
        elif sp.isscalar(other):
            warnings.warn("block-wise add ignores zero blocks!")
            return self.unary_blockwise(np.add, other)
        elif isinstance(other, np.ndarray):
            return self.to_ndarray().__add__(other)
        raise NotImplemented  # unknown type of other

    def __radd__(self, other):
        """return other + self"""
        return self.__add__(other)  # (assume commutativity of self.dtype)

    def __iadd__(self, other):
        """self += other"""
        if isinstance(other, Array):
            return self.ibinary_blockwise(np.add, other)
        elif sp.isscalar(other):
            warnings.warn("block-wise add ignores zero blocks!")
            return self.iunary_blockwise(np.add, other)
        # can't convert to numpy array in place, thus no ``self += ndarray``
        raise NotImplemented  # unknown type of other

    def __sub__(self, other):
        """return self - other"""
        if isinstance(other, Array):
            return self.binary_blockwise(np.subtract, other)
        elif sp.isscalar(other):
            warnings.warn("block-wise subtract ignores zero blocks!")
            return self.unary_blockwise(np.subtract, other)
        elif isinstance(other, np.ndarray):
            return self.to_ndarray().__sub__(other)
        raise NotImplemented  # unknown type of other

    def __isub__(self, other):
        """self -= other"""
        if isinstance(other, Array):
            return self.ibinary_blockwise(np.subtract, other)
        elif sp.isscalar(other):
            warnings.warn("block-wise subtract ignores zero blocks!")
            return self.iunary_blockwise(np.subtract, other)
        # can't convert to numpy array in place, thus no ``self -= ndarray``
        raise NotImplementedError()

    def __mul__(self, other):
        """return ``self * other`` for scalar ``other``

        Use explicit functions for matrix multiplication etc."""
        if sp.isscalar(other):
            if other == 0.:
                return self.zeros_like()
            return self.unary_blockwise(np.multiply, other)
        raise NotImplemented

    def __rmul__(self, other):
        """return ``other * self`` for scalar `other`"""
        return self * other  # (assumes commutativity of self.dtype)

    def __imul__(self, other):
        """``self *= other`` for scalar `other`"""
        if sp.isscalar(other):
            if other == 0.:
                self._data = []
                self._qdata = np.empty((0, self.rank), np.intp)
                self._qdata_sorted = True
                return self
            return self.iunary_blockwise(np.multiply, other)
        raise NotImplemented

    def __truediv__(self, other):
        """return ``self / other`` for scalar `other` with ``__future__.division``."""
        if sp.isscalar(other):
            if other == 0.:
                raise ZeroDivisionError("a/b for b=0. Types: {0!s}, {1!s}".format(
                    type(self), type(other)))
            return self.__mul__(1. / other)
        raise NotImplemented

    def __div__(self, other):
        """``self / other`` for scalar `other` without ``__future__.division``.

        Still broadcast to floats."""
        return self.__truediv__(other)

    def __itruediv__(self, other):
        """``self /= other`` for scalar `other`` with ``__future__.division``."""
        if sp.isscalar(other):
            if other == 0.:
                raise ZeroDivisionError("a/b for b=0. Types: {0!s}, {1!s}".format(
                    type(self), type(other)))
            return self.__imul__(1. / other)
        raise NotImplemented

    def __idiv__(self, other):
        """``self /= other`` for scalar `other`` without ``__future__.division``."""
        return self.__itruediv__(other)

    # private functions =======================================================

    def _set_shape(self):
        """deduce self.shape from self.legs"""
        self.shape = tuple([lc.ind_len for lc in self.legs])

    def _iter_all_blocks(self):
        """generator to iterate over all combinations of qindices in lexiographic order.

        Yields
        ------
        qindices : tuple of int
            a qindex for each of the legs
        """
        for block_inds in itertools.product(*[xrange(l.block_number)
                                              for l in reversed(self.legs)]):
            # loop over all charge sectors in lex order (last leg most siginificant)
            yield tuple(block_inds[::-1])  # back to legs in correct order

    def _get_block_charge(self, qindices):
        """returns the charge of a block selected by `qindices`

        The charge of a single block is defined as ::

            qtotal = sum_{legs l} legs[l].qind[qindices[l], 2:] * legs[l].qconj() modulo qmod
        """
        q = np.sum([l.get_charge(qi) for l, qi in itertools.izip(self.legs, qindices)], axis=0)
        return self.chinfo.make_valid(q)

    def _get_block_slices(self, qindices):
        """returns tuple of slices for a block selected by `qindices`"""
        return tuple([l.get_slice(qi) for l, qi in itertools.izip(self.legs, qindices)])

    def _get_block_shape(self, qindices):
        """return shape for the block given by qindices"""
        return tuple([(l.qind[qi, 1] - l.qind[qi, 0])
                      for l, qi in itertools.izip(self.legs, qindices)])

    def _get_block(self, qindices, insert=False, raise_incomp_q=False):
        """return the ndarray in ``_data`` representing the block corresponding to `qindices`.

        Parameters
        ----------
        qindices : 1D array of np.intp
            the qindices, for which we need to look in _qdata
        insert : bool
            If True, insert a new (zero) block, if `qindices` is not existent in ``self._data``.
            Else: just return ``None`` in that case.
        raise_incomp_q : bool
            Raise an IndexError if the charge is incompatible.

        Returns
        -------
        block: ndarray
            the block in ``_data`` corresponding to qindices
            If `insert`=False and there is not block with qindices, return ``False``

        Raises
        ------
        IndexError
            If qindices are incompatible with charge and `raise_incomp_q`
        """
        if not np.all(self._get_block_charge(qindices) == self.qtotal):
            if raise_incomp_q:
                raise IndexError("trying to get block for qindices incompatible with charges")
            return None
        # find qindices in self._qdata
        match = np.argwhere(np.all(self._qdata == qindices, axis=1))[:, 0]
        if len(match) == 0:
            if insert:
                res = np.zeros(self._get_block_shape(qindices), dtype=self.dtype)
                self._data.append(res)
                self._qdata = np.append(self._qdata, [qindices], axis=0)
                self._qdata_sorted = False
                return res
            else:
                return None
        return self._data[match[0]]

    def _bunch(self, bunch_legs):
        """Return copy and bunch the qind for one or multiple legs

        Parameters
        ----------
        bunch : list of {True, False}
            one entry for each leg, whether the leg should be bunched.

        See also
        --------
        sort_legcharge: public API calling this function.
        """
        cp = self.copy(deep=False)
        # lists for each leg:
        new_to_old_idx = [None] * cp.rank  # the `idx` returned by cp.legs[li].bunch()
        map_qindex = [None] * cp.rank  # array mapping old qindex to new qindex, such that
        # new_leg.qind[m_qindex[i]] == old_leg.qind[i]  # (except the second column entry)
        bunch_qindex = [None] * cp.rank  # bool array wheter the *new* qind was bunched
        for li, bunch in enumerate(bunch_legs):
            idx, new_leg = cp.legs[li].bunch()
            cp.legs[li] = new_leg
            new_to_old_idx[li] = idx
            # generate entries in map_qindex and bunch_qdindex
            idx = np.append(idx, [self.shape[li]])
            m_qindex = []
            bunch_qindex[li] = b_qindex = np.empty(idx.shape, dtype=np.bool_)
            for inew in xrange(len(idx) - 1):
                old_blocks = idx[inew + 1] - idx[inew]
                m_qindex.append([inew] * old_blocks)
                b_qindex[inew] = (old_blocks > 1)
            map_qindex[li] = np.concatenate(m_qindex, axis=0)

        # now map _data and _qdata
        bunched_blocks = {}  # new qindices -> index in new _data
        new_data = []
        new_qdata = []
        for old_block, old_qindices in itertools.izip(self._data, self._qdata):
            new_qindices = tuple([m[qi] for m, qi in itertools.izip(map_qindex, old_qindices)])
            bunch = any([b[qi] for b, qi in itertools.izip(bunch_qindex, new_qindices)])
            if bunch:
                if new_qindices not in bunched_blocks:
                    # create enlarged block
                    bunched_blocks[new_qindices] = len(new_data)
                    # cp has new legs and thus gives the new shape
                    new_block = np.zeros(cp._get_block_shape(new_qindices), dtype=cp.dtype)
                    new_data.append(new_block)
                    new_qdata.append(new_qindices)
                else:
                    new_block = new_data[bunched_blocks[new_qindices]]
                # figure out where to insert the in the new bunched_blocks
                old_slbeg = [l.qind[qi, 0] for l, qi in itertools.izip(self.legs, old_qindices)]
                new_slbeg = [l.qind[qi, 0] for l, qi in itertools.izip(cp.legs, new_qindices)]
                slbeg = [(o - n) for o, n in itertools.izip(old_slbeg, new_slbeg)]
                sl = [slice(beg, beg + l) for beg, l in itertools.izip(slbeg, old_block.shape)]
                # insert the old block into larger new block
                new_block[tuple(sl)] = old_block
            else:
                # just copy the old block
                new_data.append(old_block.copy())
                new_qdata.append(new_qindices)
        cp._data = new_data
        cp._qdata = np.array(new_qdata, dtype=np.intp).reshape((len(new_data), self.rank))
        cp._qsorted = False
        return cp

    def _perm_qind(self, p_qind, leg):
        """Apply a permutation `p_qind` of the qindices in leg `leg` to _qdata. In place."""
        # entry ``b`` of of old old._qdata[:, leg] refers to old ``old.legs[leg][b]``.
        # since new ``new.legs[leg][i] == old.legs[leg][p_qind[i]]``,
        # we have new ``new.legs[leg][reverse_sort_perm(p_qind)[b]] == old.legs[leg][b]``
        # thus we replace an entry `b` in ``_qdata[:, leg]``with reverse_sort_perm(q_ind)[b].
        p_qind_r = reverse_sort_perm(p_qind)
        self._qdata[:, leg] = p_qind_r[self._qdata[:, leg]]  # equivalent to
        # self._qdata[:, leg] = [p_qind_r[i] for i in self._qdata[:, leg]]
        self._qdata_sorted = False

    def _pre_indexing(self, inds):
        """check if `inds` are valid indices for ``self[inds]`` and replaces Ellipsis by slices.

        Returns
        -------
        only_integer : bool
            whether all of `inds` are (convertible to) np.intp
        inds : tuple, len=self.rank
            `inds`, where ``Ellipsis`` is replaced by the correct number of slice(None).
        """
        if type(inds) != tuple:  # for rank 1
            inds = tuple(inds)
        if len(inds) < self.rank:
            inds = inds + (Ellipsis, )
        if any([(i is Ellipsis) for i in inds]):
            fill = tuple([slice(None)] * (self.rank - len(inds) + 1))
            e = inds.index(Ellipsis)
            inds = inds[:e] + fill + inds[e + 1:]
        if len(inds) > self.rank:
            raise IndexError("too many indices for Array")
        # do we have only integer entries in `inds`?
        try:
            np.array(inds, dtype=np.intp)
        except:
            return False, inds
        else:
            return True, inds

    def _advanced_getitem(self, inds, calc_map_qind=False, permute=True):
        """calculate self[inds] for non-integer `inds`.

        This function is called by self.__getitem__(inds).
        and from _advanced_setitem_npc with ``calc_map_qind=True``.

        Parameters
        ----------
        inds : tuple
            indices for the different axes, as returned by :meth:`_pre_indexing`
        calc_map_qind :
            whether to calculate and return the additional `map_qind` and `axes` tuple

        Returns
        -------
        map_qind_part2self : function
            Only returned if `calc_map_qind` is True.
            This function takes qindices from `res` as arguments
            and returns ``(qindices, block_mask)`` such that
            ``res._get_block(part_qindices) = self._get_block(qindices)[block_mask]``.
            permutation are ignored for this.
        permutations : list((int, 1D array(int)))
            Only returned if `calc_map_qind` is True.
            Collects (axes, permutation) applied to `res` *after* `take_slice` and `iproject`.
        res : :class:`Array`
            an copy with the data ``self[inds]``.
        """
        # non-integer inds -> slicing / projection
        slice_inds = []  # arguments for `take_slice`
        slice_axes = []
        project_masks = []  # arguments for `iproject`
        project_axes = []
        permutations = []  # [axis, mask] for all axes for which we need to call `permute`
        for a, i in enumerate(inds):
            if isinstance(i, slice):
                if i != slice(None):
                    m = np.zeros(self.shape[a], dtype=np.bool_)
                    m[i] = True
                    project_masks.append(m)
                    project_axes.append(a)
                    if i.step is not None and i.step < 0:
                        permutations.append((a, np.arange(
                            np.count_nonzero(m), dtype=np.intp)[::-1]))
            else:
                try:
                    iter(i)
                except:  # not iterable: single index
                    slice_inds.append(int(i))
                    slice_axes.append(a)
                else:  # iterable
                    i = np.asarray(i)
                    project_masks.append(i)
                    project_axes.append(a)
                    if i.dtype != np.bool_:  # should be integer indexing
                        perm = np.argsort(i)  # check if maks is sorted
                        if np.any(perm != np.arange(len(perm))):
                            # np.argsort(i) gives the reverse permutation, so reverse it again.
                            # In that way, we get the permuation within the projected indices.
                            permutations.append((a, reverse_sort_perm(perm)))
        res = self.take_slice(slice_inds, slice_axes)
        res_axes = np.cumsum([(a not in slice_axes) for a in xrange(self.rank)]) - 1
        p_map_qinds, p_masks = res.iproject(project_masks, [res_axes[p] for p in project_axes])
        permutations = [(res_axes[a], p) for a, p in permutations]
        if permute:
            for a, perm in permutations:
                res = res.permute(perm, a)
        if not calc_map_qind:
            return res
        part2self = self._advanced_getitem_map_qind(inds, slice_axes, slice_inds, project_axes,
                                                    p_map_qinds, p_masks, res_axes)
        return part2self, permutations, res

    def _advanced_getitem_map_qind(self, inds, slice_axes, slice_inds, project_axes, p_map_qinds,
                                   p_masks, res_axes):
        """generate a function mapping from qindices of `self[inds]` back to qindices of self

        This function is called only by `_advanced_getitem(calc_map_qind=True)`
        to obtain the function `map_qind_part2self`,
        which in turn in needed in `_advanced_setitem_npc` for ``self[inds] = other``.
        This function returns a function `part2self`, see doc string in the source for details.
        Note: the function ignores permutations introduced by `inds` - they are handled separately.
        """
        map_qinds = [None] * self.rank
        map_blocks = [None] * self.rank
        for a, i in zip(slice_axes, slice_inds):
            qi, within_block = self.legs[a].get_qindex(inds[a])
            map_qinds[a] = qi
            map_blocks[a] = within_block
        for a, m_qind in zip(project_axes, p_map_qinds):
            map_qinds[a] = np.nonzero(m_qind >= 0)[0]  # revert m_qind
        # keep_axes = neither in slice_axes nor in project_axes
        keep_axes = [a for a, i in enumerate(map_qinds) if i is None]
        not_slice_axes = sorted(project_axes + keep_axes)
        bsizes = [l._get_block_sizes() for l in self.legs]

        def part2self(part_qindices):
            """given `part_qindices` of ``res = self[inds]``,
            return (`qindices`, `block_mask`) such that
            ``res._get_block(part_qindices) == self._get_block(qindices)``.
            """
            qindices = map_qinds[:]  # copy
            block_mask = map_blocks[:]  # copy
            for a in keep_axes:
                qindices[a] = qi = part_qindices[res_axes[a]]
                block_mask[a] = np.arange(bsizes[a][qi], dtype=np.intp)
            for a, bmask in zip(project_axes, p_masks):
                old_qi = part_qindices[res_axes[a]]
                qindices[a] = map_qinds[a][old_qi]
                block_mask[a] = bmask[old_qi]
            # advanced indexing in numpy is tricky ^_^
            # np.ix_ can't handle integer entries reducing the dimension.
            # we have to call it only on the entries with arrays
            ix_block_mask = np.ix_(*[block_mask[a] for a in not_slice_axes])
            # and put the result back into block_mask
            for a, bm in zip(not_slice_axes, ix_block_mask):
                block_mask[a] = bm
            return qindices, tuple(block_mask)

        return part2self

    def _advanced_setitem_npc(self, inds, other):
        """self[inds] = other for non-integer `inds` and :class:`Array` `other`.
        This function is called by self.__setitem__(inds, other)."""
        map_part2self, permutations, self_part = self._advanced_getitem(
            inds, calc_map_qind=True, permute=False)
        # permuations are ignored by map_part2self.
        # instead of figuring out permuations in self, apply the *reversed* permutations ot other
        for ax, perm in permutations:
            other = other.permute(reverse_sort_perm(perm), ax)
        # now test compatibility of self_part with `other`
        if self_part.rank != other.rank:
            raise IndexError("wrong number of indices")
        for pl, ol in zip(self_part.legs, other.legs):
            pl.test_contractible(ol.conj())
        if np.any(self_part.qtotal != other.qtotal):
            raise ValueError("wrong charge for assinging self[inds] = other")
        # note: a block exists in self_part, if and only if its extended version exists in self.
        # by definition, non-existent blocks in `other` are zero.
        # instead of checking which blocks are non-existent,
        # we first set self[inds] completely to zero
        for p_qindices in self_part._qdata:
            qindices, block_mask = map_part2self(p_qindices)
            block = self._get_block(qindices)
            block[block_mask] = 0.  # overwrite data in self
        # now we copy blocks from other
        for o_block, o_qindices in zip(other._data, other._qdata):
            qindices, block_mask = map_part2self(o_qindices)
            block = self._get_block(qindices, insert=True)
            block[block_mask] = o_block  # overwrite data in self
        self.ipurge_zeros(0.)  # remove blocks identically zero

    def _combine_legs_make_pipes(self, combine_legs, pipes, qconj):
        """argument parsing for :meth:`combine_legs`: make missing pipes.

        Generates missing pipes & checks compatibility for provided pipes."""
        npipes = len(combine_legs)
        # default arguments for pipes and qconj
        if pipes is None:
            pipes = [None] * npipes
        elif len(pipes) != npipes:
            raise ValueError("wrong len of `pipes`")
        qconj = list(toiterable(qconj if qconj is not None else +1))
        if len(qconj) == 1 and 1 < npipes:
            qconj = [qconj[0]] * npipes  # same qconj for all pipes
        if len(qconj) != npipes:
            raise ValueError("wrong len of `qconj`")

        pipes = list(pipes)
        # make pipes as necessary
        for i, pipe in enumerate(pipes):
            if pipe is None:
                pipes[i] = self.make_pipe(axes=combine_legs[i], qconj=qconj[i])
            else:
                # test for compatibility
                legs = [self.legs[a] for a in combine_legs[i]]
                if pipe.nlegs != len(legs):
                    raise ValueError("pipe has wrong number of legs")
                if legs[0].qconj != pipe.legs[0].qconj:
                    pipes[i] = pipe = pipe.conj()  # need opposite qind
                for self_leg, pipe_leg in zip(legs, pipe.legs):
                    self_leg.test_contractible(pipe_leg.conj())
        return pipes

    def _combine_legs_new_axes(self, combine_legs, new_axes):
        """figure out new_axes and how legs have to be transposed"""
        all_combine_legs = np.concatenate(combine_legs)
        non_combined_legs = np.array([a for a in range(self.rank) if a not in all_combine_legs])
        if new_axes is None:  # figure out default new_legs
            first_cl = np.array([cl[0] for cl in combine_legs])
            new_axes = [(np.sum(non_combined_legs < a) + np.sum(first_cl < a)) for a in first_cl]
        else:  # test compatibility
            if len(new_axes) != len(combine_legs):
                raise ValueError("wrong len of `new_axes`")
            new_rank = len(combine_legs) + len(non_combined_legs)
            for i, a in enumerate(new_axes):
                if a < 0:
                    new_axes[i] = a + new_rank
                elif a >= new_rank:
                    raise ValueError("new_axis larger than the new number of legs")
        transp = [[a] for a in non_combined_legs]
        for s in np.argsort(new_axes):
            transp.insert(new_axes[s], list(combine_legs[s]))
        transp = sum(transp, [])  # flatten: [a] + [b] = [a, b]
        return new_axes, tuple(transp)

    def _combine_legs_worker(self, combine_legs, new_axes, pipes):
        """the main work of combine_legs: create a copy and reshape the data blocks.

        Assumes standard form of parameters.

        Parameters
        ----------
        combine_legs : list(1D np.array)
            axes of self which are collected into pipes.
        new_axes : 1D array
            the axes of the pipes in the new array. Ascending.
        pipes : list of :class:`LegPipe`
            all the correct output pipes, already generated.

        Returns
        -------
        res : :class:`Array`
            copy of self with combined legs
        """
        all_combine_legs = np.concatenate(combine_legs)
        # non_combined_legs: axes of self which are not in combine_legs
        non_combined_legs = np.array(
            [a for a in range(self.rank) if a not in all_combine_legs], dtype=np.intp)
        legs = [self.legs[i] for i in non_combined_legs]
        for na, p in zip(new_axes, pipes):  # not reversed
            legs.insert(na, p)
        non_new_axes = [i for i in range(len(legs)) if i not in new_axes]
        non_new_axes = np.array(non_new_axes, dtype=np.intp)  # for index tricks

        res = self.copy(deep=False)
        res.legs = legs
        res._set_shape()
        res.labels = {}
        # map `self._qdata[:, combine_leg]` to `pipe.q_map` indices for each new pipe
        qmap_inds = [p._map_incoming_qind(self._qdata[:, cl])
                     for p, cl in zip(pipes, combine_legs)]

        # get new qdata
        qdata = np.empty((self.stored_blocks, res.rank), dtype=self._qdata.dtype)
        qdata[:, non_new_axes] = self._qdata[:, non_combined_legs]
        for na, p, qmap_ind in zip(new_axes, pipes, qmap_inds):
            np.take(p.q_map[:, -1],  # last column of q_map maps to qindex of the pipe
                    qmap_ind,
                    out=qdata[:, na])  # write the result directly into qdata
        # now we have probably many duplicate rows in qdata,
        # since for the pipes many `qmap_ind` map to the same `qindex`
        # find unique entries by sorting qdata
        sort = np.lexsort(qdata.T)
        qdata_s = qdata[sort]
        old_data = [self._data[s] for s in sort]
        qmap_inds = [qm[sort] for qm in qmap_inds]
        # divide into parts, which give a single new block
        diffs = charges._find_row_differences(qdata_s)  # including the first and last row

        # now the hard part: map data
        data = []
        slices = [slice(None)] * res.rank  # for selecting the slices in the new blocks
        # iterate over ranges of equal qindices in qdata_s
        for beg, end in itertools.izip(diffs[:-1], diffs[1:]):
            qindices = qdata_s[beg]
            new_block = np.zeros(res._get_block_shape(qindices), dtype=res.dtype)
            data.append(new_block)
            # copy blocks
            for old_data_idx in xrange(beg, end):
                for na, p, qm_ind in zip(new_axes, pipes, qmap_inds):
                    slices[na] = slice(*p.q_map[qm_ind[old_data_idx], :2])
                sl = tuple(slices)
                new_block_view = new_block[sl]
                # reshape block while copying
                new_block_view[:] = old_data[old_data_idx].reshape(new_block_view.shape)
        res._qdata = qdata_s[diffs[:-1]]  # (keeps the dimensions)
        res._qdata_sorted = True
        res._data = data
        return res

    def _split_legs_worker(self, split_axes, cutoff):
        """the main work of split_legs: create a copy and reshape the data blocks.

        Called by :meth:`split_legs`. Assumes that the corresponding legs are LegPipes.
        """
        # calculate mappings of axes
        # in self
        split_axes = np.array(sorted(split_axes), dtype=np.intp)
        pipes = [self.legs[a] for a in split_axes]
        nonsplit_axes = np.array(
            [i for i in xrange(self.rank) if i not in split_axes], dtype=np.intp)
        # in result
        new_nonsplit_axes = np.arange(self.rank, dtype=np.intp)
        for a in reversed(split_axes):
            new_nonsplit_axes[a + 1:] += self.legs[a].nlegs - 1
        new_split_axes_first = new_nonsplit_axes[split_axes]  # = the first leg for splitted pipes
        new_split_slices = [slice(a, a + p.nlegs) for a, p in zip(new_split_axes_first, pipes)]
        new_nonsplit_axes = new_nonsplit_axes[nonsplit_axes]

        res = self.copy(deep=False)
        legs = res.legs
        for a in reversed(split_axes):
            legs[a:a + 1] = legs[a].legs  # replace pipes with saved original legs
        res._set_shape()

        # get new qdata by stacking columns
        tmp_qdata = np.empty((self.stored_blocks, res.rank), dtype=np.intp)
        tmp_qdata[:, new_nonsplit_axes] = self._qdata[:, nonsplit_axes]
        tmp_qdata[:, new_split_axes_first] = self._qdata[:, split_axes]

        # now split the blocks
        data = []
        qdata = []  # rows of the new qdata
        new_block_shape = np.empty(res.rank, dtype=np.intp)
        block_slice = [slice(None)] * self.rank
        for old_block, qdata_row in itertools.izip(self._data, tmp_qdata):
            qmap_slices = [p.q_map_slices[i]
                           for p, i in zip(pipes, qdata_row[new_split_axes_first])]
            new_block_shape[new_nonsplit_axes] = np.array(old_block.shape)[nonsplit_axes]
            for qmap_rows in itertools.product(*qmap_slices):
                for a, sl, qm, pipe in zip(split_axes, new_split_slices, qmap_rows, pipes):
                    qdata_row[sl] = block_qind = qm[2:-1]
                    new_block_shape[sl] = [(l.qind[qi, 1] - l.qind[qi, 0])
                                           for l, qi in zip(pipe.legs, block_qind)]
                    block_slice[a] = slice(qm[0], qm[1])
                new_block = old_block[block_slice].reshape(new_block_shape)
                # all charges are compatible by construction, but some might be zero
                if not np.any(np.abs(new_block) > cutoff):
                    continue
                data.append(new_block.copy())  # copy, not view
                qdata.append(qdata_row.copy())  # copy! qdata_row is changed afterwards...
        if len(data) > 0:
            res._qdata = np.array(qdata, dtype=np.intp)
            res._qdata_sorted = False
        else:
            res._qdata = np.empty((0, res.rank), dtype=np.intp)
            res._qdata_sorted = True
        res._data = data
        return res

    def _split_leg_label(self, label, count):
        """Revert the combination of labels performed in :meth:`_combine_legs`.

        Return a list of labels corresponding to the original labels before 'combine_legs'.
        Test that it splits into `count` labels.

        Examples
        --------
        >>> self._split_leg_label('(a,b,(c,d))', 3)
        ['a', 'b', '(c.d)']
        """
        if label[0] != '(' or label[-1] != ')':
            raise ValueError("split label, which is not of the Form '(...)'")
        beg = 1
        depth = 0  # number of non-closed '(' to the left
        res = []
        for i in range(1, len(label) - 1):
            c = label[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            elif c == '.' and depth == 0:
                res.append(label[beg:i])
                beg = i + 1
        res.append(label[beg:i + 1])
        if len(res) != count:
            raise ValueError("wrong number of splitted labels.")
        for i in xrange(len(res)):
            if res[i][0] == '?':
                res[i] = None
        return res

    def _conj_leg_label(self, label):
        """conjugate a leg `label`.

        Takes ``'a' -> 'a*'; 'a*'-> 'a'; '(a.(b*.c))' -> '(a*.(b.c*))'``"""
        # first insert '*' after each label, taking into account recursion of LegPipes
        res = []
        beg = 0
        for i in range(1, len(label)):
            if label[i - 1] != ')' and label[i] in '.)':
                res.append(label[beg:i])
                beg = i
        res.append(label[beg:])
        label = '*'.join(res)
        if label[-1] != ')':
            label += '*'
        # remove '**' entries
        return label.replace('**', '')


# functions ====================================================================


def zeros(*args, **kwargs):
    """create a npc array full of zeros (with no _data).

    This is just a wrapper around ``Array(...)``,
    detailed documentation can be found in the class doc-string of :class:`Array`."""
    return Array(*args, **kwargs)


def eye_like(a, axis=0):
    """An identity matrix contractible with the axis of `a`."""
    axis = a.get_leg_index(axis)
    return diag(1., a.legs[axis])


def diag(s, leg, dtype=None):
    """Returns a square, diagonal matrix of entries `s`.

    The resulting matrix has legs ``(leg, leg.conj())`` and charge 0.

    Parameters
    ----------
    s : scalar | 1D array
        the entries to put on the diagonal. If scalar, all diagonal entries are the same.
    leg : :class:`LegCharge`
        the first leg of the resulting matrix.
    dtype : None | type
        the data type to be used for the result. By default, use dtype of `s`.

    Returns
    -------
    diagonal : :class:`Array`
        a square matrix with diagonal entries `s`.

    See also
    --------
    :meth:`Array.scale_axis` : similar as ``tensordot(diag(s), ...)``, but faster.
    """
    s = np.asarray(s, dtype)
    scalar = (s.ndim == 0)
    if not scalar and len(s) != leg.ind_len:
        raise ValueError("len(s)={0:d} not equal to leg.ind_len={1:d}".format(len(s), leg.ind_len))
    res = Array(leg.chinfo, (leg, leg.conj()), s.dtype)  # default charge is 0
    # qdata = [[0, 0], [1, 1], ....]
    res._qdata = np.arange(leg.block_number, dtype=np.intp)[:, np.newaxis] * np.ones(2, np.intp)
    # ``res._qdata_sorted = True`` was already set
    if scalar:
        res._data = [np.diag(s*np.ones(size, dtype=s.dtype)) for size in leg._get_block_sizes()]
    else:
        res._data = [np.diag(s[leg.get_slice(qi)]) for qi in xrange(leg.block_number)]
    return res


def concatenate(arrays, axis=0, copy=True):
    """stack arrays along a given axis, similar as np.concatenate.

    Stacks the qind of the array, without sorting/blocking.
    Labels are inherited from the first array only.

    Parameters
    ----------
    arrays : iterable of :class:`Array`
        the arrays to be stacked. They must have the same shape and charge data
        except on the specified axis.
    axis : int | str
        leg index or label of the first array. Defines the axis along which the arrays are stacked.
    copy : bool
        wheter to copy the data blocks

    Returns
    -------
    stacked : :class:`Array`
        concatenation of the given `arrays` along the specified axis.

    See also
    --------
    :meth:`Array.sort_legcharge` : can be used to block by charges along the axis.
    """
    arrays = list(arrays)
    res = arrays[0].zeros_like()
    res.labels = arrays[0].labels.copy()
    axis = res.get_leg_index(axis)
    not_axis = range(res.rank)
    del not_axis[axis]
    not_axis = np.array(not_axis, dtype=np.intp)
    # test for compatibility
    for a in arrays:
        if a.shape[:axis] != res.shape[:axis] or a.shape[axis+1:] != res.shape[axis+1:]:
            raise ValueError("wrong shape "+repr(a))
        if a.chinfo != res.chinfo:
            raise ValueError("wrong ChargeInfo")
        if a.qtotal != res.qtotal:
            raise ValueError("wrong qtotal")
        for l in not_axis:
            a.legs[l].test_equal(res.legs[l])
    dtype = res.dtype = np.find_common_type([a.dtype for a in arrays], [])
    # stack the data
    res_axis_qinds = []
    res_qdata = []
    res_data = []
    ind_shift = 0  # sum of previous `ind_len`
    qind_shift = 0  # sum of previous `block_number`
    axis_qconj = res.legs[axis].qconj
    for a in arrays:
        leg = a.legs[axis]
        # shift first two columns of `leg.qind`
        qind = leg.qind.copy()
        qind[:, :2] += ind_shift
        if leg.qconj != axis_qconj:
            qind[:, 2:] = res.chinfo.make_valid(-qind[:, 2:])
        res_axis_qinds.append(qind)
        qdata = a._qdata.copy()
        qdata[:, axis] += qind_shift
        res_qdata.append(qdata)
        if copy:
            res_data.extend([np.array(t, dtype) for t in a._data])
        else:
            res_data.extend([np.asarray(t, dtype) for t in a._data])
        # update shifts for next array
        ind_shift += leg.ind_len
        qind_shift += leg.block_number
    res_axis_qinds = np.concatenate(res_axis_qinds, axis=0)
    res.legs[axis] = LegCharge.from_qind(res.chinfo, res_axis_qinds, axis_qconj)
    res._set_shape()
    res._qdata = np.concatenate(res_qdata, axis=0)
    res._qdata_sorted = False
    res._data = res_data
    res.test_sanity()
    return res


def grid_concat(grid, axes, copy=True):
    """Given an np.array of npc.Arrays, performs a multi-dimensional concatentation along 'axes'.

    Stacks the qind of the array, *without* sorting/blocking.

    Parameters
    ----------
    grid : array_like of :class:`Array`
        the grid of arrays.
    axes : list of int
        The axes along which to concatenate the arrays,  same len as the dimension of the grid.
        Concatenate arrays of the `i`th axis of the grid along the axis ``axes[i]``
    copy : bool
        whether the _data blocks are copied.

    Examples
    --------
    Assume we have rank 2 Arrays ``A, B, C, D`` of shapes
    ``(1, 2), (1, 4), (3, 2), (3, 4)`` sharing the legs of equal sizes.
    Then the following grid will result in a ``(1+3, 2+4)`` shaped array:

    >>> g = grid_concat([[A, B], [C, D]], axes=[0, 1])
    >>> g.shape
    (4, 6)

    If ``A, B, C, D`` were rank 4 arrays, with the first and last leg as before, and sharing
    *common* legs ``1`` and ``2``, then you would get a rank-4 array:

    >>> g = grid_concat([[A, B], [C, D]], axes=[0, 3])
    >>> g.shape
    (4, 6)

    See also
    --------
    :meth:`Array.sort_legcharge` : can be used to block by charges.
    """
    if not isinstance(grid, np.ndarray):
        grid = np.array(grid, dtype=np.object)
    if grid.ndim < 1 or grid.ndim != len(axes):
        raise ValueError("grid has wrong dimension")
    # Simple recursion on ndim. Copy only required on first go.
    if grid.ndim > 1:
        grid = [grid_concat(b, axes=axes[1:], copy=copy) for b in grid]
        copy = False
    grid = concatenate(grid, axes[0], copy=copy)
    return grid


def grid_outer(grid, grid_legs, qtotal=None):
    """Given an np.array of npc.Arrays, return the corresponding higher-dimensional Array.

    Parameters
    ----------
    grid : array_like of {:class:`Array` | None}
        the grid gives the first part of the axes of the resulting array.
        Entries have to have all the same shape and charge-data, giving the remaining axes.
        ``None`` entries in the grid are interpreted as zeros.
    grid_legs : list of :class:`LegCharge`
        One LegCharge for each dimension of the grid along the grid.
    qtotal : charge
        The total charge of the Array.
        By default (``None``), derive it out from a non-trivial entry of the grid.

    Returns
    -------
    res : :class:`Array`
        An Array with shape ``grid.shape + nontrivial_grid_entry.shape``.
        Constructed such that ``res[idx] == grid[idx]`` for any index ``idx`` of the `grid`
        the `grid` entry is not trivial (``None``).

    See also
    --------
    grid_outer_calc_legcharge : can calculate one missing :class:`LegCharge` of the grid.


    Examples
    --------
    A typical use-case for this function is the generation of an MPO.
    Say you have npc.Arrays ``Splus, Sminus, Sz``, each with legs ``[phys.conj(), phys]``.
    Further, you have to define appropriate LegCharges `l_left` and `l_right`.
    Then one 'matrix' of the MPO for a nearest neighbour Heisenberg Hamiltonian could look like:


    >>> id = np.eye_like(Sz)
    >>> W_mpo = grid_outer([[id, Splus, Sminus, Sz, None],
    ...                     [None, None, None, None, J*Sminus],
    ...                     [None, None, None, None, J*Splus],
    ...                     [None, None, None, None, J*Sz],
    ...                     [None, None, None, None, id]],
    ...                    leg_charges=[l_left, l_right])
    >>> W_mpo.shape
    (4, 4, 2, 2)

    .. todo :
        Would be really nice, if it could derive appropriate leg charges at least for one leg.
        derived from the entries
    """
    grid_shape, entries = _nontrivial_grid_entries(grid)
    if len(grid_shape) != len(grid_legs):
        raise ValueError("wrong number of grid_legs")
    if grid_shape != tuple([l.ind_len for l in grid_legs]):
        raise ValueError("grid shape incompatible with grid_legs")
    idx, entry = entries[0]  # first non-trivial entry
    chinfo = entry.chinfo
    dtype = np.find_common_type([e.dtype for _, e in entries], [])
    legs = list(grid_legs) + entry.legs
    if qtotal is None:
        # figure out qtotal from first non-zero entry
        grid_charges = [l.get_charge(l.get_qindex(i)[0]) for i, l in zip(idx, grid_legs)]
        qtotal = chinfo.make_valid(np.sum(grid_charges + [entry.qtotal], axis=0))
    else:
        qtotal = chinfo.make_valid(qtotal)
    res = Array(entry.chinfo, legs, dtype, qtotal)
    # main work: iterate over all non-trivial entries to fill `res`.
    for idx, entry in entries:
        res[idx] = entry  # insert the values with Array.__setitem__ partial slicing.
    res.test_sanity()
    return res


def grid_outer_calc_legcharge(grid, grid_legs, qtotal=None, qconj=1, bunch=False):
    """Derive a LegCharge for a grid used for :func:`grid_outer`.

    Note: the resulting LegCharge is *not* bunched.

    Parameters
    ----------
    grid : array_like of {:class:`Array` | None}
        the grid as it will be given to :func:`grid_outer`
    grid_legs : list of {:class:`LegCharge` | None}
        One LegCharge for each dimension of the grid, except for one entry which is ``None``.
        This missing entry is to be calculated.
    qtotal : charge
        The desired total charge of the array. Defaults to 0.

    Returns
    -------
    new_grid_legs : list of :class:`LegCharge`
        A copy of the given `grid_legs` with the ``None`` replaced by a compatible LegCharge.
    """
    grid_shape, entries = _nontrivial_grid_entries(grid)
    if len(grid_shape) != len(grid_legs):
        raise ValueError("wrong number of grid_legs")
    if any([s != l.ind_len for s, l in zip(grid_shape, grid_legs) if l is not None]):
        raise ValueError("grid shape incompatible with grid_legs")
    idx, entry = entries[0]  # first non-trivial entry
    chinfo = entry.chinfo
    axis = [a for a, l in enumerate(grid_legs) if l is None]
    if len(axis) > 1:
        raise ValueError("can only derive one grid_leg")
    axis = axis[0]
    grid_legs = list(grid_legs)
    qtotal = chinfo.make_valid(qtotal)  # charge 0, if qtotal is not set.
    qflat = [None]*grid_shape[axis]
    for idx, entry in entries:
        grid_charges = [l.get_charge(l.get_qindex(i)[0])
                        for a, (i, l) in enumerate(zip(idx, grid_legs)) if a != axis]
        qflat_entry = chinfo.make_valid(qtotal - entry.qtotal - np.sum(grid_charges, axis=0))
        i = idx[axis]
        if qflat[i] is None:
            qflat[i] = qflat_entry
        elif np.any(qflat[i] != qflat_entry):
            print qflat
            print qflat[i]
            print qflat_entry
            raise ValueError("different grid entries lead to different charges" +
                             " at index " + str(i))
    if any([q is None for q in qflat]):
        raise ValueError("can't derive flat charge for all indices:" + str(qflat))
    grid_legs[axis] = LegCharge.from_qflat(chinfo, qconj*np.array(qflat), qconj)
    return grid_legs


def outer(a, b):
    """Forms the outer tensor product, equivalent to ``tensordot(a, b, axes=0)``.

    Labels are inherited from `a` and `b`. In case of a collision (same label in both `a` and `b`),
    they are both dropped.

    Parameters
    ----------
    a, b : :class:`Array`
        the arrays for which to form the product.

    Returns
    -------
    c : :class:`Array`
        Array of rank ``a.rank + b.rank`` such that (for ``Ra = a.rank; Rb = b.rank``):

            c[i_1, ..., i_Ra, j_1, ... j_R] = a[i_1, ..., i_Ra] * b[j_1, ..., j_rank_b]
    """
    if a.chinfo != b.chinfo:
        raise ValueError("different ChargeInfo")
    dtype = np.find_common_type([a.dtype, b.dtype], [])
    qtotal = a.chinfo.make_valid(a.qtotal + b.qtotal)
    res = Array(a.chinfo, a.legs+b.legs, dtype, qtotal)

    # fill with data
    qdata_a = a._qdata
    qdata_b = b._qdata
    grid = np.mgrid[:len(qdata_a), :len(qdata_b)].T.reshape(-1, 2)
    # grid is lexsorted like qdata, with rows as all combinations of a/b block indices.
    qdata_res = np.empty((len(qdata_a)*len(qdata_b), res.rank), dtype=np.intp)
    qdata_res[:, :a.rank] = qdata_a[grid[:, 0]]
    qdata_res[:, a.rank:] = qdata_b[grid[:, 1]]
    # use numpys broadcasting to obtain the tensor product
    idx_reshape = (Ellipsis,) + tuple([np.newaxis]*b.rank)
    data_a = [ta[idx_reshape] for ta in a._data]
    idx_reshape = tuple([np.newaxis]*a.rank) + (Ellipsis,)
    data_b = [tb[idx_reshape] for tb in b._data]
    res._data = [data_a[i] * data_b[j] for i, j in grid]
    res._qdata = qdata_res
    res._qdata_sorted = a._qdata_sorted and b._qdata_sorted  # since grid is lex sorted
    # labels
    res.labels = a.labels.copy()
    for k in b.labels:
        if k in res.labels:
            del res.labels[k]  # drop collision
        else:
            res.labels[k] = b.labels[k] + a.rank
    return res


def inner(a, b, axes=None, do_conj=False):
    """Contract all legs in `a` and `b`, return scalar.

    Parameters
    ----------
    a, b : class:`Array`
        The arrays for which to calculate the product.
        Must have same rank, and compatible LegCharges.
    axes : ``(axes_a, axes_b)`` | ``None``
        ``None`` is equivalent to ``(range(-axes, 0), range(axes))``.
        Alternatively, `axes_a` and `axes_b` specifiy the legs of `a` and `b`, respectively,
        which should be contracted. Legs can be specified with leg labels or indices.
        Contract leg ``axes_a[i]`` of `a` with leg ``axes_b[i]`` of `b`.
    do_conj : bool
        If ``False`` (Default), ignore it.
        if ``True``, conjugate `a` before, i.e., return ``inner(a.conj(), b, axes)``

    Returns
    -------
    inner_product : dtype
        a scalar (of common dtype of `a` and `b`) giving the full contraction of `a` and `b`.
    """
    if a.rank != b.rank:
        raise ValueError("different rank!")
    if axes is not None:
        axes_a, axes_b = axes
        axes_a = a.get_leg_indices(toiterable(axes_a))
        axes_b = a.get_leg_indices(toiterable(axes_b))
        # we can permute axes_a and axes_b. Use that to ensure axes_b = range(b.rank)
        sort_axes_b = np.argsort(axes_b)
        axes_a = [axes_a[i] for i in sort_axes_b]
        transp = (tuple(axes_a) != tuple(range(a.rank)))
    else:
        transp = False
    if transp or do_conj:
        a = a.copy(deep=False)
    if transp:
        a.itranspose(axes_a)
    if do_conj:
        a = a.iconj()
    # check charge compatibility
    if a.chinfo != b.chinfo:
        raise ValueError("different ChargeInfo")
    for lega, legb in zip(a.legs, b.legs):
        lega.test_contractible(legb)
    dtype = np.find_common_type([a.dtype, b.dtype], [])
    res = dtype.type(0)
    if any(a.chinfo.make_valid(a.qtotal + b.qtotal) != 0):
        return res  # can't have blocks to be contracted
    if a.stored_blocks == 0 or b.stored_blocks == 0:
        return res  # also trivial

    # need to find common blocks in a and b, i.e. equal leg charges.
    # for faster comparison, generate 1D arrays with a combined index
    stride = np.cumprod([1] + [l.block_number for l in a.legs[:-1]])
    a_qdata = np.sum(a._qdata*stride, axis=1)
    a_data = a._data
    if not a._qdata_sorted:
        perm = np.argsort(a_qdata)
        a_qdata = a_qdata[perm]
        a_data = [a_data[i] for i in perm]
    b_qdata = np.sum(b._qdata*stride, axis=1)
    b_data = b._data
    if not b._qdata_sorted:
        perm = np.argsort(b_qdata)
        b_qdata = b_qdata[perm]
        b_data = [b_data[i] for i in perm]
    for i, j in _iter_common_sorted(a_qdata, b_qdata,
                                    xrange(len(a_qdata)), xrange(len(b_qdata))):
        res += np.inner(a_data[i].reshape((-1,)), b_data[j].reshape((-1,)))
    return res


def tensordot(a, b, axes=2):
    """Similar as ``np.tensordot`` but for :class:`Array`.

    Builds the tensor product of `a` and `b` and sums over the specified axes.
    Does not require complete blocking of the charges.

    Labels are inherited from `a` and `b`.
    In case of a collistion (= the same label inherited from `a` and `b`), both labels are dropped.

    Parameters
    ----------
    a, b : :class:`Array`
        the first and second npc Array for which axes are to be contracted.
    axes : ``(axes_a, axes_b)`` | int
        A single integer is equivalent to ``(range(-axes, 0), range(axes))``.
        Alternatively, `axes_a` and `axes_b` specifiy the legs of `a` and `b`, respectively,
        which should be contracted. Legs can be specified with leg labels or indices.
        Contract leg ``axes_a[i]`` of `a` with leg ``axes_b[i]`` of `b`.

    Returns
    -------
    a_dot_b : :class:`Array`
        The tensorproduct of `a` and `b`, summed over the specified axes.
        In case of a full contraction, returns scalar.

    Implementation Notes
    --------------------
    Looking at the source of numpy's tensordot (which is just 62 lines of python code),
    you will find that it has the following strategy:
    1. Transpose `a` and `b` such that the axes to sum over are in the end of `a` and front of `b`.
    2. Combine the legs `axes`-legs and other legs with a `np.reshape`,
       such that `a` and `b` are matrices.
    3. Perform a matrix product with `np.dot`.
    4. Split the remaining axes with another `np.reshape` to obtain the correct shape.

    The main work is done by `np.dot`, which calls LAPACK to perform the simple matrix product.
    [This matrix multiplication of a ``NxK`` times ``KxM`` matrix is actually faster
    than the O(N*K*M) needed by a naive implementation looping over the indices.]

    We follow the same overall strategy, viewing the :class:`Array` as a tensor with
    data block entries.
    Step 1) is performed directly in this function body.

    The steps 2) and 4) could be implemented with :meth:`Array.combine_legs`
    and :meth:`Array.split_legs`.
    However, that would actually be an overkill: we're not interested
    in the full charge data of the combined legs (which would be generated in the LegPipes).
    Instead, we just need to track the qindices of the `a._qdata` and `b._qdata` carefully.

    Our step 2) is implemented in :func:`_tensordot_pre_worker`:
    We split `a._qdata` in `a_qdata_keep` and `a_qdata_sum`, and similar for `b`.
    Then, view `a` is a matrix :math:`A_{i,k1}` and `b` as :math:`B_{k2,j}`, where
    `i` can be any row of `a_qdata_keep`, `j` can be any row of `b_qdata_keep`.
    The `k1` and `k2` are rows of `a_qdata_sum` and `b_qdata_sum`, which stem from the same legs
    (up to a :meth:`LegCharge.conj()`).
    In our storage scheme, `a._data[s]` then contains the block :math:`A_{i,k1}` for
    ``j = a_qdata_keep[s]`` and ``k1 = a_qdata_sum[s]``.
    To identify the different indices `i` and `j`, it is easiest to lexsort in the `s`.
    Note that we give priority to the `#_qdata_keep` over the `#_qdata_sum`, such that
    equal rows of `i` are contiguous in `#_qdata_keep`.
    Then, they are identified with :func:`Charges._find_row_differences`.

    Now, the goal is to calculate the sums :math:`C_{i,j} = sum_k A_{i,k} B_{k,j}`,
    analogous to step 3) above. This is implemented in :func:`_tensordot_worker`.
    It is done 'naively' by explicit loops over ``i``, ``j`` and ``k``.
    However, this is not as bad as it sounds:
    First, we loop only over existent ``i`` and ``j``
    (in the sense that there is at least some non-zero block with these ``i`` and ``j``).
    Second, if the ``i`` and ``j`` are not compatible with the new total charge,
    we know that ``C_{i,j}`` will be zero.
    Third, given ``i`` and ``j``, the sum over ``k`` runs only over
    ``k1`` with nonzero :math:`A_{i,k1}`, and ``k2` with nonzero :math:`B_{k2,j}`.

    How many multiplications :math:`A_{i,k} B_{k,j}` we actually have to perform
    depends on the sparseness. In the ideal case, if ``k`` (i.e. a LegPipe of the legs summed over)
    is completely blocked by charge, the 'sum' over ``k`` will contain at most one term!

    Step 4) is - as far as necessary - done in parallel with step 3).
    """
    if a.chinfo != b.chinfo:
        raise ValueError("Different ChargeInfo")
    try:
        axes_a, axes_b = axes
        axes_int = False
    except TypeError:
        axes = int(axes)
        axes_int = True
    if not axes_int:
        # like step 1.) bring into standard form by transposing
        axes_a = a.get_leg_indices(toiterable(axes_a))
        axes_b = b.get_leg_indices(toiterable(axes_b))
        if len(axes_a) != len(axes_a):
            raise ValueError("different lens of axes for a, b: " + repr(axes))
        not_axes_a = [i for i in range(a.rank) if i not in axes_a]
        not_axes_b = [i for i in range(b.rank) if i not in axes_b]
        a = a.copy(deep=False)
        b = b.copy(deep=False)
        a.itranspose(not_axes_a + axes_a)
        b.itranspose(axes_b + not_axes_b)
        axes = len(axes_a)
    # now `axes` is integer
    # check for special cases
    if axes == 0:
        return outer(a, b)  # no sum necessary
    if axes == a.rank and axes == b.rank:
        return inner(a, b)  # full contraction

    # check for contraction compatibility
    for lega, legb in zip(a.legs[-axes:], b.legs[:axes]):
        lega.test_contractible(legb)

    # the main work is out-sourced
    res = _tensordot_worker(a, b, axes)

    # labels
    res.labels = a.labels.copy()
    for k in b.labels:
        if k in res.labels:
            del res.labels[k]  # drop collision
        else:
            res.labels[k] = b.labels[k] + a.rank - 2*axes
    return res


def norm(a, ord=None, convert_to_float=True):
    r"""Norm of flattened data.

    Equivalent to ``np.linalg.norm(a.to_ndarray().flatten(), ord)``.

    In contrast to numpy, we don't distinguish between matrices and vectors,
    but simply calculate the norm for the **flat** (block) data.
    The usual `ord`-norm is defined as  :math:`(\sum_i |a_i|^{ord} )^{1/ord}`.

    ==========  ======================================
    ord         norm
    ==========  ======================================
    None/'fro'  Frobenius norm (same as 2-norm)
    inf         ``max(abs(x))``
    -inf        ``min(abs(x))``
    0           ``sum(a != 0) == np.count_nonzero(x)``
    other       ususal `ord`-norm
    ==========  ======================================

    Parameters
    ----------
    a : :class:`Array` | np.ndarray
        the array of which the norm should be calculated.
    ord :
        the order of the norm. See table above.
    convert_to_float :
        convert integer to float before calculating the norm, avoiding int overflow

    Returns
    -------
    norm : float
        the norm over the *flat* data of the array.
    """
    if isinstance(a, Array):
        return a.norm(ord, convert_to_float)
    elif isinstance(a, np.ndarray):
        if convert_to_float:
            new_type = np.find_common_type([np.float_, a.dtype], [])  # int -> float
            a = np.asarray(a, new_type)  # doesn't copy, if the dtype did not change.
        return np.linalg.norm(a.reshape((-1,)), ord)
    else:
        raise ValueError("unknown type of a")


# private functions ============================================================

def _nontrivial_grid_entries(grid):
    """return a list [(idx, entry)] of non-``None`` entries in an array_like grid."""
    grid = np.asarray(grid, dtype=np.object)
    entries = []  # fill with (multi_index, entry)
    # use np.nditer to iterate with multi-index over the grid.
    # see https://docs.scipy.org/doc/numpy/reference/arrays.nditer.html for details.
    it = np.nditer(grid, flags=['multi_index', 'refs_ok'])  # numpy iterator
    while not it.finished:
        e = it[0].item()
        if e is not None:
            entries.append((it.multi_index, e))
        it.iternext()
    if len(entries) == 0:
        raise ValueError("No non-trivial entries in grid")
    return grid.shape, entries


def _iter_common_sorted(a, b, a_idx, b_idx):
    """Yields ``i, j for j, i in itertools.product(b_idx, a_idx) if a[i] == b[j]``.

    *Assumes* that ``[a[i] for i in a_idx]`` and ``[b[j] for j in b_idx]`` are strictly ascending.
    Given that, it is equivalent to (but faster than)::

        for j, i in itertools.product(b_idx, a_idx):
            if a[i] == b[j]:
                yield i, j
    """
    a_it = iter(a_idx)
    b_it = iter(b_idx)
    i = next(a_it)
    j = next(b_it)
    try:
        while True:
            if a[i] < b[j]:
                i = next(a_it)
            elif b[j] < a[i]:
                j = next(b_it)
            else:
                yield i, j
                i = next(a_it)
                j = next(b_it)
    except StopIteration:
        pass  # only one of the iterators finished
    for i in a_it:      # remaing in a_it. skipped if a_it is finished.
        if a[i] == b[j]:
            yield i, j
    for j in b_it:      # remaining in b_it
        if a[i] == b[j]:
            yield i, j
    raise StopIteration  # finished


def _tensordot_pre_worker(a, b, cut_a, cut_b):
    """The pre-calculations before the actual matrix procut.

    Called by :func:`_tensordot_worker`.
    See doc-string of :func:`tensordot` for details on the implementation.
    """
    # convert qindices over which we sum to a 1D array for faster lookup/iteration
    stride = np.cumprod([1] + [l.block_number for l in a.legs[cut_a:-1]])
    a_qdata_sum = np.sum(a._qdata[:, cut_a:]*stride, axis=1)
    # lex-sort a_qdata, dominated by the axes kept, then the axes summed over.
    a_sort = np.lexsort(np.append(a_qdata_sum[:, np.newaxis], a._qdata[:, :cut_a], axis=1).T)
    a_qdata_keep = a._qdata[a_sort, :cut_a]
    a_qdata_sum = a_qdata_sum[a_sort]
    a_data = a._data
    a_data = [a_data[i] for i in a_sort]
    # combine all b_qdata[axes_b] into one column (with the same stride as before)
    b_qdata_sum = np.sum(b._qdata[:, :cut_b] * stride, axis=1)
    # lex-sort b_qdata, dominated by the axes summed over, then the axes kept.
    b_data = b._data
    if not b._qdata_sorted:
        b_sort = np.lexsort(np.append(b_qdata_sum[:, np.newaxis], b._qdata[:, cut_b:], axis=1).T)
        b_qdata_keep = b._qdata[b_sort, cut_b:]
        b_qdata_sum = b_qdata_sum[b_sort]
        b_data = [b_data[i] for i in b_sort]
    else:
        b_qdata_keep = b._qdata[:, cut_b:]
    # find blocks where qdata_a[not_axes_a] and qdata_b[not_axes_b] change
    a_slices = charges._find_row_differences(a_qdata_keep)
    b_slices = charges._find_row_differences(b_qdata_keep)
    a_qdata_keep = a_qdata_keep[a_slices[:-1]]
    b_qdata_keep = b_qdata_keep[b_slices[:-1]]
    a_charges_keep = a.chinfo.make_valid(
        np.sum([l.get_charge(qi) for l, qi in zip(a.legs[:cut_a], a_qdata_keep.T)], axis=0))
    b_charges_keep = a.chinfo.make_valid(
        np.sum([l.get_charge(qi) for l, qi in zip(b.legs[cut_b:], b_qdata_keep.T)], axis=0))
    # collect and return the results
    a_pre_result = a_data, a_qdata_sum, a_qdata_keep, a_charges_keep, a_slices
    b_pre_result = b_data, b_qdata_sum, b_qdata_keep, b_charges_keep, b_slices
    return a_pre_result, b_pre_result


def _tensordot_worker(a, b, axes):
    """main work of tensordot.

    Assumes standard form of parameters: axes is integer,
    sum over the last `axes` legs of `a` and first `axes` legs of `b`.

    Called by :func:`tensordot`.
    See doc-string of :func:`tensordot` for details on the implementation.
    """
    cut_a = a.rank - axes
    cut_b = axes
    a_pre_result, b_pre_result = _tensordot_pre_worker(a, b, cut_a, cut_b)
    a_data, a_qdata_sum, a_qdata_keep, a_charges_keep, a_slices = a_pre_result
    b_data, b_qdata_sum, b_qdata_keep, b_charges_keep, b_slices = b_pre_result
    chinfo = a.chinfo
    qtotal = chinfo.make_valid(a.qtotal + b.qtotal)
    dtype = np.find_common_type([a.dtype, b.dtype], [])
    res_qdata = []
    res_data = []
    # loop over column/row of the result
    for col_b, b_qindex_keep in enumerate(b_qdata_keep):
        # (row_a changes faster than col_b, such that the resulting array is qdata lex-sorted)
        Q_col = b_charges_keep[col_b]
        b_sl = xrange(*b_slices[col_b:col_b+2])
        for row_a, a_qindex_keep in enumerate(a_qdata_keep):
            Q_row = a_charges_keep[row_a]
            if np.any(chinfo.make_valid(Q_col + Q_row) != qtotal):
                continue
            a_sl = xrange(*a_slices[row_a:row_a+2])
            block_sum = None
            for k1, k2 in _iter_common_sorted(a_qdata_sum, b_qdata_sum, a_sl, b_sl):
                block = np.tensordot(a_data[k1], b_data[k2], axes=axes)
                # TODO: optimize? # reshape, dot, reshape.
                if block_sum is None:
                    block_sum = np.asarray(block, dtype=dtype)
                else:
                    block_sum += block
            if block_sum is None:
                continue  # no common blocks
            res_qdata.append(np.append(a_qindex_keep, b_qindex_keep, axis=0))
            res_data.append(block_sum)
    res = Array(chinfo, a.legs[:cut_a]+b.legs[cut_b:], dtype, qtotal)
    if len(res_data) == 0:
        return res
    # (at least one of Q_row, Q_col is non-empty, so _qdata is also not empty)
    res._qdata = np.array(res_qdata, dtype=np.intp)
    res._qdata_sorted = True
    res._data = res_data
    print res._qdata.shape
    print res.stored_blocks
    print res.rank
    print res.shape
    res.test_sanity()
    return res