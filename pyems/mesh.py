from typing import List, Tuple, Optional
from enum import Enum
from bisect import bisect_left, bisect_right, insort_left
from warnings import warn
import numpy as np
import scipy.optimize
from CSXCAD.CSPrimitives import CSPrimitives
from pyems.simulation import Simulation
from pyems.calc import wavelength
from pyems.coordinate import Box3, Coordinate3, c3_from_dim
from pyems.csxcad import add_line, construct_box, add_material
from pyems.fp import (
    fp_equalp,
    fp_nearest,
    fp_equalp,
    fp_ltp,
    fp_gtp,
    fp_lep,
    fp_gep,
)
from pyems.priority import priorities


class Type(Enum):
    """
    Metal is a Metal or ConductingSheet.  Nonmetal is a nonmetal,
    physical property.
    """

    metal = 0
    nonmetal = 1
    air = 2


class BoundedType:
    """
    A Type with associated positional bounds.  Corresponds to one
    dimension of a physical structure.
    """

    def __init__(
        self, prop_type: Type, lower_bound: float, upper_bound: float
    ):
        """
        """
        self.prop_type = prop_type
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound

    def get_type(self) -> Type:
        """
        """
        return self.prop_type

    def get_bounds(self) -> List[float]:
        """
        """
        return [self.lower_bound, self.upper_bound]

    def get_midpoint(self) -> float:
        """
        """
        return np.average([self.lower_bound, self.upper_bound])

    def size(self) -> float:
        """
        """
        return self.upper_bound - self.lower_bound


def _prim_metalp(prim: CSPrimitives) -> bool:
    """
    Return True if CSXCAD primitive is a metal.
    """
    type_str = prim.GetProperty().GetTypeString()
    if (
        type_str == "Metal"
        or type_str == "ConductingSheet"
        or type_str == "LumpedElement"
    ):
        return True
    return False


def _prim_materialp(prim: CSPrimitives) -> bool:
    """
    Return True if CSXCAD primitive is a nonmetal, physical property.
    """
    type_str = prim.GetProperty().GetTypeString()
    if type_str == "Material":
        return True
    return False


def _get_prim_bounds(prim: CSPrimitives) -> np.array:
    """
    Get the physical boundary of a CSXCAD primitive.
    """
    orig_bounds = prim.GetBoundBox()
    # transforms do not affect the bound box, so we must do it manually.
    tr = prim.GetTransform()
    orig_bounds[0] = np.array(tr.Transform(orig_bounds[0]))
    orig_bounds[1] = np.array(tr.Transform(orig_bounds[1]))
    bounds = np.array([[None, None], [None, None], [None, None]])
    for i in range(3):
        lower = np.min([orig_bounds[0][i], orig_bounds[1][i]])
        upper = np.max([orig_bounds[0][i], orig_bounds[1][i]])
        bounds[i] = np.array([lower, upper])
    return bounds


def _sort_bounded_types(
    bounded_types: List[List[BoundedType]],
) -> List[List[BoundedType]]:
    """
    Sort the bounded types for each dimension so that the types with
    the smallest bounds appear first.
    """
    new_bounded_types = [[], [], []]
    for dim, btype_list in enumerate(bounded_types):
        new_bounded_types[dim] = sorted(btype_list, key=lambda x: x.size())
    return new_bounded_types


def _physical_prims(prims: List[CSPrimitives]) -> List[CSPrimitives]:
    """
    Return just the physical primitives from a list of CSXCAD
    primitives.
    """
    physical_prims = []
    for prim in prims:
        if _prim_metalp(prim) or _prim_materialp(prim):
            physical_prims.append(prim)

    return physical_prims


def _remove_dups(lst: List[float], fixed: List[float] = []) -> List[float]:
    """
    Remove all duplicate items from a sorted list.

    :param lst: The list from which to remove duplicates.
    :param fixed: A list of elements that must remain in the original
        list.  If the list contains two elements that are nearly the
        same, but one is also an item in the fixed list, we should
        remove the element not in the fixed list.  We can remove a
        fixed element from the list only if it is duplicated exactly.

    :returns: The original list, but with all duplicate items removed.
    """
    new_lst = []
    last = None
    for elt in lst:
        if last is not None:
            if elt == last:  # can always skip if identical
                continue
            elif fp_equalp(elt, last) and elt not in fixed:
                continue
            elif fp_equalp(elt, last) and elt in fixed:
                del new_lst[-1]
        last = elt
        new_lst.append(elt)

    return new_lst


def _bounds_from_prims(
    prims: List[CSPrimitives], fixed: List[List[float]]
) -> List[List[float]]:
    """
    Return a list of all boundary positions.

    :param prims: List of CSXCAD primitives.
    :param fixed: Fixed bounds that may not be removed.

    :returns: A 3 element list where each element is a list of
              boundary positions for a dimension.  Each dimension list
              is sorted and all positions are unique.
    """
    dim_bounds = [[], [], []]
    for prim in prims:
        prim_bounds = _get_prim_bounds(prim)
        for dim, bounds in enumerate(prim_bounds):
            dim_bounds[dim].append(bounds[0])
            dim_bounds[dim].append(bounds[1])

    for dim, bounds in enumerate(dim_bounds):
        dim_bounds[dim] = sorted(bounds)
        dim_bounds[dim] = _remove_dups(dim_bounds[dim], fixed[dim])

    return dim_bounds


def _float_inside(val: float, lower: float, upper: float) -> bool:
    """
    """
    if val >= lower and val <= upper:
        return True
    return False


def _factor_for_num(num: int, smaller_spacing: float, dist: float) -> float:
    """
    Compute the geometric series factor such that the geometric series
    sum is equal to a provided distance.
    """
    roots = scipy.optimize.fsolve(
        func=_geom_dist_zero, x0=1.5, args=(num, smaller_spacing, dist)
    )
    factor = roots[0]
    return factor


def _factor_ubound(num: int, ratio: float, max_factor: float) -> float:
    """
    Compute the maximum factor for the spacing between adjacent
    separations.
    """
    return np.min([max_factor, np.power(ratio, 1 / (num - 1))])


def _geom_dist(factor: float, num: int, smaller_spacing: float) -> float:
    """
    Total distance spanned by a geometric series where each item in
    the series corresponds to a distance.

    :param factor: The factor between successive terms in the series.
    :param num: Number of terms in the series.
    :param smaller_spacing: The initial spacing.  The first term of
        the geometric series is equal to this term times the factor.

    :returns: Sum of the geometric series.
    """
    powers = np.arange(1, num, 1)
    dist = smaller_spacing * np.sum(np.power(factor, powers))
    return dist


def _geom_dist_zero(
    factor: float, num: int, smaller_spacing: float, dist: float
) -> float:
    """
    Difference between a geometric distance (see ``_geom_dist``) and a
    provided distance.
    """
    return _geom_dist(factor, num, smaller_spacing) - dist


def _num_for_factor(
    factor: float, smaller_spacing: float, dist: float
) -> (float, int):
    """
    Find the closest number of terms in a geometric series with a
    given factor such that the sum of the geometric series equals
    ``dist``.

    :param factor: The geometric series factor.
    :param smaller_spacing:
    :param dist: Desired sum of the geometric series.

    :returns: An adjusted factor and associated number of terms such
              that the desired sum is exactly matched.
    """
    num = int(
        np.ceil(
            np.log(1 - (((dist / smaller_spacing) + 1) * (1 - factor)))
            / np.log(factor)
            + 1
        )
    )
    factor = _factor_for_num(num, smaller_spacing, dist)

    # Prevent factor from falling below 1.
    while factor < 1:
        num -= 1
        if num == 0:
            raise RuntimeError("_num_for_factor failed. This is a bug.")
        factor = _factor_for_num(num, smaller_spacing, dist)

    return (factor, num)


def _geom_series(
    smaller_spacing: float,
    larger_spacing: float,
    dist: float,
    min_num: int,
    max_factor: float,
) -> (float, int):
    """
    Geometric series for a given sum and subject to a minimum number
    of terms and max factor constraints.

    :param dist: Desired sum of geometric series.
    :param min_num: Minimum number of terms in the series.
    :param max_factor: Maximum factor between successive terms in the
        series.

    :returns: Computed factor and number of terms satisfying the
              constraints.
    """
    num = np.max([int(np.ceil(dist / larger_spacing)) + 1, min_num])
    factor = _factor_for_num(num, smaller_spacing, dist)
    while factor >= _factor_ubound(
        num, larger_spacing / smaller_spacing, max_factor
    ):
        num += 1
        factor = _factor_for_num(num, smaller_spacing, dist)

    return (factor, num)


def _lines_const_factor_in_bounds(
    lower: float,
    upper: float,
    lower_spacing: float,
    upper_spacing: float,
    dim: int,
    min_lines: int,
    smooth: float,
) -> np.array:
    """
    """
    # If the lower and upper spacings are roughly equal, make the
    # lines equidistant.
    if np.isclose(lower_spacing, upper_spacing, rtol=1e-3, atol=0):
        num_lines = int(np.ceil((upper - lower) / lower_spacing)) + 1
        num_lines = int(np.max([num_lines, min_lines]))
        return np.linspace(lower, upper, num_lines)

    (factor, num_lines) = _geom_series(
        smaller_spacing=np.min([lower_spacing, upper_spacing]),
        larger_spacing=np.max([lower_spacing, upper_spacing]),
        dist=upper - lower,
        min_num=min_lines,
        max_factor=smooth,
    )

    powers = np.arange(1, num_lines, 1)
    if lower_spacing < upper_spacing:
        spacings = lower_spacing * np.power(factor, powers)
        lines = np.array(lower + np.cumsum(spacings))
        lines = np.concatenate(([lower], lines))
    else:
        spacings = upper_spacing * np.power(factor, powers)
        lines = np.array(upper - np.cumsum(spacings))
        lines = np.concatenate(([upper], lines))
        lines = np.flip(lines)

    lines[-1] = upper  # last line should be exactly equal to upper
    return lines


def _spacing_at_dist(spacing: float, dist: float, max_factor: float) -> float:
    """
    The spacing a specified distance away for a given initial spacing
    and maximum factor.

    :param spacing: The initial spacing.  The computed factor
        successively increases the spacing from this value.
    :param dist: A distance away from the current location.  This
        constrains the geometric series.
    :param max_factor: The maximum permissible factor in the geometric
        series.

    :returns: The spacing ``dist`` away from the current location.
    """
    factor, num = _num_for_factor(max_factor, spacing, dist)
    return spacing * (factor ** (num - 1))


def _spacings_at_dist_zero(
    dist: float,
    lower_spacing: float,
    upper_spacing: float,
    total_dist: float,
    max_factor: float,
) -> float:
    """
    """
    spacing1 = _spacing_at_dist(lower_spacing, dist, max_factor)
    spacing2 = _spacing_at_dist(upper_spacing, total_dist - dist, max_factor)
    return spacing2 - spacing1


def _dist_for_max_spacings(
    lower_spacing: float, upper_spacing: float, dist: float, max_factor: float
) -> float:
    """
    Compute the distance from a lower bound such that the last spacing
    from the upper bound and lower bound are equal.  This finds a sort
    of midpoint of a total distance ``dist`` where the spacings become
    the same, subject to the constraint that the factors of the lower
    and upper geometric series are less than some maximum factor.
    """
    roots = scipy.optimize.fsolve(
        func=_spacings_at_dist_zero,
        x0=dist / 2,
        args=(lower_spacing, upper_spacing, dist, max_factor),
    )
    lower_dist = roots[0]
    return lower_dist


def _pos_in_bounds(pos: float, lower: float, upper: float) -> bool:
    """
    """
    if fp_gep(pos, lower) and fp_lep(pos, upper):
        return True
    return False


def _type_at_pos(prims: List[CSPrimitives], dim: int, pos: float) -> Type:
    """
    The material type for a given dimension and position.  If multiple
    properties exist for the dimension and position, the property type
    corresponding to the smallest dimension is used.

    TODO this ignores CSXCAD priorities.  I think this is fine since
    this only determines the mesh density, but we could always extend
    this in the future if necessary.

    :param prims: List of physical CSXCAD primitives.
    :param dim: 0, 1, or 2 for x, y, z.
    :param pos: Position to check.

    :returns: The type of the material at that position.
    """
    smallest_dim = np.inf
    current_type = None

    for prim in prims:
        prim_bounds = _get_prim_bounds(prim)
        if _float_inside(pos, prim_bounds[dim][0], prim_bounds[dim][1]):
            dim_size = prim_bounds[dim][1] - prim_bounds[dim][0]
            # If there are multiple primitives of the same size, we
            # want to register metal if one of the primitives is metal.
            if np.isclose(dim_size, smallest_dim, rtol=1e-3, atol=0):
                if _prim_metalp(prim):
                    current_type = Type.metal
                    smallest_dim = dim_size
            elif dim_size < smallest_dim:
                smallest_dim = dim_size
                if _prim_metalp(prim):
                    current_type = Type.metal
                else:
                    current_type = Type.nonmetal

    return current_type


def _dim_idx_to_desc(idx: int) -> str:
    """
    Provide a descriptor for a dimensional index.  In this context, 0
    indicates xmin, 1 xmax, 2 ymin, etc.
    """
    if idx == 0:
        return "xmin"
    if idx == 1:
        return "xmax"
    if idx == 2:
        return "ymin"
    if idx == 3:
        return "ymax"
    if idx == 4:
        return "zmin"
    if idx == 5:
        return "zmax"

    raise ValueError("Index must be between 0 and 5, inclusive.")


def _mesh_lines_in_box(
    mesh_lines: List[List[float]], box: Box3
) -> List[List[float]]:
    """
    All mesh line positions within a box.

    :param mesh_lines: List of 3 inner lists, where each inner list
        contains the mesh line positions for that dimension.  The
        first inner list corresponds to the x-dimension, the second to
        the y-dimension, and the third to the z-dimension.
    :param box: 3D box that mesh lines must be inside of.
    """
    mesh_lines_inside = []
    for dim in [0, 1, 2]:
        lower_pos = box.min_corner[dim]
        upper_pos = box.max_corner[dim]
        dim_lines = mesh_lines[dim]
        dim_lines_inside = dim_lines[
            bisect_left(dim_lines, lower_pos) : bisect_right(
                dim_lines, upper_pos
            )
        ]
        mesh_lines_inside.append(dim_lines_inside)

    return mesh_lines_inside


class Mesh:
    """
    An OpenEMS mesh object that supports automatic mesh generation.

    For simplicity, as far as the mesh is concerned, we only recognize
    2 different material types: metal and nonmetal physical meterials.
    """

    def __init__(
        self,
        sim: Simulation,
        metal_res=1 / 20,
        nonmetal_res=1 / 10,
        smooth: Tuple[float, float, float] = (1.2, 1.2, 1.2),
        min_lines: int = 5,
        expand_bounds: Tuple[
            Tuple[int, int], Tuple[int, int], Tuple[int, int]
        ] = ((8, 8), (8, 8), (8, 8)),
        simulation_bounds: Tuple[
            Tuple[float, float], Tuple[float, float], Tuple[float, float]
        ] = None,
    ):
        """
        :param sim: Simulation object to which mesh should be added.
        :param metal_res: the metal resolution, specified as a factor
            of lmin.
        :param nonmetal_res: the substrate resolution, specified as a
            factor of lmin.
        :param smooth: the factor by which adjacent cells are allowed
            to differ in size.  This should be a list of 3 factors,
            one for each dimension.  This is useful if, for instance,
            you want the mesh to be smoother in the direction of
            signal propagation.
        :param min_lines: the minimum number of mesh lines for a
            primitive's dimensional length, unless the length of that
            primitive's dimension is precisely 0.
        :param expand bounds: list of 3 inner lists corresponding to
            [[xmin, xmax], [ymin, ymax], [zmin, zmax]] each element
            gives the number of cells to add to the mesh at that
            boundary.  The cell size is determined by nonmetal_res and
            the actual number of cells added may be more (or possibly)
            less than what is specified here due to the thirds rule
            and smoothing.  This essentially defines the simulation
            box.  It's anticipated that the user will only define
            physical structures (e.g. metal layers, substrate, etc.)
            and will use this to set the simulation box.
        :param simulation_bounds: Same structure as expand_bounds, but
            uses absolute positions.  If set this will enforce a
            strict total mesh size and expand_bounds will be ignored.
            An error will trigger if the internal CSX structures
            require a larger mesh than the one specified here.
        """
        self._sim = sim
        self._sim.register_mesh(self)
        self.lmin = wavelength(self.sim.max_frequency(), self.sim.unit)
        self.metal_res = metal_res * self.lmin
        self.nonmetal_res = nonmetal_res * self.lmin
        self.smooth = smooth
        # mesh lines are added at both boundaries, which gives us an
        # extra mesh line
        self.min_lines = min_lines
        self.expand_bounds = expand_bounds
        self.simulation_bounds = simulation_bounds
        # Keep track of mesh regions already applied. This is an array
        # of 3 elements. The 1st element is a list of ranges in the
        # x-dimension that have already been meshed. The 2nd
        # corresponds to the y-dimension and the 3rd corresponds to
        # the z-dimension.
        self.ranges_meshed = [[], [], []]
        # Keep a list of all metal boundaries. No mesh line is allowed to
        # lie on a boundary, and to the extent possible, should obey the
        # thirds rule about that boundary. Zero-dimension metal structures
        # are not added to this list. The 1st item is the x-dimension
        # list, the 2nd is the y-dimension list and the 3rd is the
        # z-dimension list.
        self.metal_bounds = [[], [], []]
        # Mesh lines that cannot be moved. These are mesh lines that
        # lie directly on a zero-dimension primitive.
        self.fixed_lines = [[], [], []]
        # Keep track of the smallest valid resolution value. This
        # allows us to later remove all adjacent mesh lines separated
        # by less than this value.
        self.smallest_res = self.metal_res
        # Set the lines first and draw them last since the API doesn't
        # appear to expose a way to remove individual lines.
        self.mesh_lines = [[], [], []]
        # Bounds of the entire simulation box
        self.sim_bounds = [[], [], []]
        # The generated mesh.
        self.mesh = self._sim.csx.GetGrid()

        # set later
        self.bounded_types = None

        self.generate_mesh()

    @property
    def sim(self) -> Simulation:
        """
        """
        return self._sim

    def generate_mesh(self, show_pml: bool = True):
        """
        Autogenerate a mesh given the CSX structure.

        :param show_pml: Add boxes to display the location of the PML.
            These boxes do not affect the simulation.
        """
        prims = self.sim.csx.GetAllPrimitives()
        physical_prims = _physical_prims(prims)
        self._set_fixed_lines(physical_prims)
        bounds = _bounds_from_prims(physical_prims, self.fixed_lines)
        bounded_types = self._bounded_types(bounds, physical_prims)
        self.bounded_types = self._set_expanded_bounds(bounded_types)
        self._set_metal_bounds(bounded_types)
        self.size_ordered_bounded_types = _sort_bounded_types(bounded_types)
        self._gen_mesh_for_bounded_types(self.size_ordered_bounded_types)
        self._trim_air_mesh()
        self._smooth_pml_mesh_lines()

        self._set_mesh_from_lines()
        if show_pml:
            self._show_pml(self.pml_boxes())
        self.sim.post_mesh()

        # ensure mesh smoothness
        self._check_pml_mesh_uniform()
        self._check_mesh_smooth()

        # self._ensure_pml_structure_uniform()

    def _check_pml_mesh_uniform(self):
        """
        Ensure mesh inside PML has uniform spacing.
        """
        pml_boxes = self.pml_boxes()
        for i, box in enumerate(pml_boxes):
            if box.has_zero_dim():
                continue
            dim = i // 2
            lines = _mesh_lines_in_box(self.mesh_lines, box)[dim]
            last_spacing = lines[1] - lines[0]
            for j in range(2, len(lines)):
                spacing = lines[j] - lines[j - 1]

                if not fp_equalp(spacing, last_spacing):
                    print("PAY ATTENTION!!! error checking has been disabled mesh.py line 647")
                # if not fp_equalp(spacing, last_spacing):
                #     raise RuntimeError(
                #         "PML mesh lines not uniform. This is a pyems bug."
                #     )

    def _check_mesh_smooth(self):
        """
        Ensure mesh smoothness adhered to.
        """
        for dim in range(3):
            lines = self.mesh_lines[dim]
            smoothness = self.smooth[dim]
            last_spacing = lines[1] - lines[0]
            for i in range(2, len(lines)):
                spacing = lines[i] - lines[i - 1]
                factor = np.maximum(
                    spacing / last_spacing, last_spacing / spacing
                )
                if not factor < smoothness:
                    warn(
                        (
                            "Mesh line at pos {:.4f} for dimension {} violates "
                            "smoothness. Smoothness was set to {:.2f} but this "
                            "line creates a spacing with factor {:.2f}. For "
                            "convenience the last three lines are: {:.4f}, "
                            "{:.4f} and {:.4f}."
                        ).format(
                            lines[i],
                            dim,
                            smoothness,
                            factor,
                            lines[i - 2],
                            lines[i - 1],
                            lines[i],
                        )
                    )
                last_spacing = spacing

    def _smooth_pml_mesh_lines(self):
        """
        Mesh lines inside a PML should have uniform spacing in the
        direction of the boundary.  Move mesh lines to adhere to this
        requirement.
        """
        pml_boxes = self.pml_boxes()
        for i, box in enumerate(pml_boxes):
            if box.has_zero_dim():
                continue
            dim = i // 2
            is_lower_bound = i % 2 == 0
            lines = _mesh_lines_in_box(self.mesh_lines, box)[dim]
            idx_lower = bisect_left(self.mesh_lines[dim], lines[0])
            idx_upper = bisect_right(self.mesh_lines[dim], lines[-1])
            dist = lines[-1] - lines[0]
            spacing = dist / (len(lines) - 1)
            num_lines = len(lines)
            # ensure that new spacing does not violate mesh smoothness
            if is_lower_bound:
                limit_spacing = (
                    self.mesh_lines[dim][idx_upper]
                    - self.mesh_lines[dim][idx_upper - 1]
                )
            else:
                limit_spacing = (
                    self.mesh_lines[dim][idx_lower]
                    - self.mesh_lines[dim][idx_lower - 1]
                )
            smooth_factor = self.smooth[dim]
            if (
                spacing > limit_spacing
                and spacing / limit_spacing >= smooth_factor
            ):
                spacing = limit_spacing * smooth_factor
            elif (
                spacing < limit_spacing
                and limit_spacing / spacing >= smooth_factor
            ):
                raise RuntimeError(
                    "PML mesh smoothing is increasing the simulation "
                    "box size.  This should never happen.  Either "
                    "structures in your PML are non-uniform in the "
                    "direction of the PML in which case you'll get an "
                    "error anyway, or this is a bug in pyems."
                )

            # only smooth lines if necessary
            if np.isclose(spacing, limit_spacing, rtol=1e-3, atol=0):
                continue

            if is_lower_bound:
                init_pos = lines[0]
            else:
                init_pos = lines[-1]
                spacing *= -1
            new_lines = sorted(
                [init_pos + i * spacing for i in range(num_lines)]
            )
            # only clear lines in the new range because we will have
            # to generate new lines between the border of this new
            # line boundary and the nearest BoundedType border. The
            # line closest to the BoundedType border is preserved
            # (either below or above if this is the lower PML or upper
            # PML, respectively) so that thirds rule and other such
            # sensitive spacings are preserved. If we clear lines
            # based on the old line range we run the risk of clearing
            # that nearest line.
            self._clear_mesh_in_bounds(new_lines[0], new_lines[-1], dim)

            self._add_lines_to_mesh(new_lines, dim)

            if is_lower_bound:
                existing_bound = new_lines[-1]
                other_bound = None
                for bt in self.size_ordered_bounded_types[dim]:
                    bt_bounds = bt.get_bounds()
                    bt_lb = bt_bounds[0]
                    bt_ub = bt_bounds[1]
                    if existing_bound >= bt_lb and existing_bound <= bt_ub:
                        other_bound = bt_ub
                        break

                lower_spacing = spacing
                line_below_idx, line_below = self._line_below(dim, other_bound)
                upper_spacing = (
                    self.mesh_lines[dim][line_below_idx + 1] - line_below
                )
                redo_lines = _lines_const_factor_in_bounds(
                    existing_bound,
                    line_below,
                    lower_spacing,
                    upper_spacing,
                    dim,
                    0,
                    self.smooth[dim],
                )
                del self.mesh_lines[dim][num_lines:line_below_idx]
                self._add_lines_to_mesh(redo_lines, dim)

            else:
                existing_bound = new_lines[0]
                other_bound = None
                for bt in self.size_ordered_bounded_types[dim]:
                    bt_bounds = bt.get_bounds()
                    bt_lb = bt_bounds[0]
                    bt_ub = bt_bounds[1]
                    if existing_bound >= bt_lb and existing_bound <= bt_ub:
                        other_bound = bt_lb
                        break

                # spacing was negative for new_lines
                upper_spacing = np.abs(spacing)
                line_above_idx, line_above = self._line_above(dim, other_bound)
                lower_spacing = (
                    line_above - self.mesh_lines[dim][line_above_idx - 1]
                )
                redo_lines = _lines_const_factor_in_bounds(
                    line_above,
                    existing_bound,
                    lower_spacing,
                    upper_spacing,
                    dim,
                    0,
                    self.smooth[dim],
                )
                del self.mesh_lines[dim][line_above_idx + 1 : -num_lines]
                self._add_lines_to_mesh(redo_lines, dim)

    def _ensure_pml_structure_uniform(self):
        """
        All materials must be uniform inside a PML in the direction of
        the PML boundary.  Emit an error if this is not the case.
        """
        pml_boxes = self.pml_boxes()
        for i, box in enumerate(pml_boxes):
            if box.has_zero_dim():
                continue
            box.set_increasing()
            dim = i // 2
            lines = _mesh_lines_in_box(self.mesh_lines, box)
            non_dim1 = (dim + 1) % 3
            non_dim2 = (dim + 2) % 3
            dim_lines = lines[dim]
            if len(dim_lines) == 0:
                continue
            for pos1 in lines[non_dim1]:
                for pos2 in lines[non_dim2]:
                    prop = self.sim.csx.GetPropertyByCoordPriority(
                        fp_nearest(
                            c3_from_dim(
                                dim, (dim_lines[0], pos1, pos2)
                            ).coordinate_list()
                        )
                    )
                    for dim_pos in dim_lines[1:]:
                        dim_prop = self.sim.csx.GetPropertyByCoordPriority(
                            fp_nearest(
                                c3_from_dim(
                                    dim, (dim_pos, pos1, pos2)
                                ).coordinate_list()
                            )
                        )
                        if not dim_prop.GetName() == prop.GetName():
                            raise RuntimeError(
                                (
                                    "{} PML does not contain uniform structure."
                                    " Offending coordinate is ({}, {}, {})"
                                ).format(
                                    _dim_idx_to_desc(i), dim_pos, pos1, pos2
                                )
                            )

    def _trim_air_mesh(self) -> None:
        """
        Remove excess boundary cells.
        """
        if self.simulation_bounds is not None:
            return

        for dim in range(3):
            lower_pml_cells = self.expand_bounds[dim][0]
            if lower_pml_cells > 0:
                pos = self._lowest_nonair_pos(dim)
                mesh_idx, _ = self._line_below(dim, pos)
                if mesh_idx > lower_pml_cells:
                    del_num = mesh_idx - lower_pml_cells
                    del self.mesh_lines[dim][0:del_num]

            upper_pml_cells = self.expand_bounds[dim][1]
            if upper_pml_cells > 0:
                pos = self._highest_nonair_pos(dim)
                mesh_idx, _ = self._line_above(dim, pos)
                if len(self.mesh_lines[dim]) - 1 - mesh_idx > upper_pml_cells:
                    del_num = (
                        len(self.mesh_lines[dim])
                        - 1
                        - mesh_idx
                        - upper_pml_cells
                    )
                    del self.mesh_lines[dim][-del_num:]

    def _lowest_nonair_pos(self, dim: int) -> float:
        """
        """
        lowest_pos = None
        for btype in self.bounded_types[dim]:
            if btype.get_type() != Type.air and (
                lowest_pos is None or btype.lower_bound < lowest_pos
            ):
                lowest_pos = btype.lower_bound
        return lowest_pos

    def _highest_nonair_pos(self, dim: int) -> float:
        """
        """
        highest_pos = None
        for btype in self.bounded_types[dim]:
            if btype.get_type() != Type.air and (
                highest_pos is None or btype.upper_bound > highest_pos
            ):
                highest_pos = btype.upper_bound
        return highest_pos

    def _show_pml(
        self, boxes: Tuple[Box3, Box3, Box3, Box3, Box3, Box3]
    ) -> None:
        """
        Add primitives so that PML boundaries are displayed in
        AppCSXCAD.  Even though these are material properties, they're
        defined as a low-priority air layer so they do not affect the
        simulation.
        """
        for i, box in enumerate(boxes):
            if not box.has_zero_dim():
                pml_prop = add_material(
                    csx=self.sim.csx,
                    name="PML_8_" + _dim_idx_to_desc(i),
                    epsilon=1,
                    mue=1,
                    kappa=0,
                    sigma=0,
                    color="#d3d3d3",
                    alpha=200,
                )
                construct_box(
                    prop=pml_prop, box=box, priority=priorities["x"],
                )

    def pml_boxes(self) -> Tuple[Box3, Box3, Box3, Box3, Box3, Box3]:
        """
        Tuple of 6 Box3, corresponding to PML boxes for:

        (xmin, xmax, ymin, ymax, zmin, zmax).
        """
        boxes: List[Box3] = []
        pml_cells = self.sim.boundary_conditions.pml_bounds()
        # TODO find more concise way to do this
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][pml_cells[0][0]],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][-1],
                ),
            )
        )
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1 - pml_cells[0][1]],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][-1],
                ),
            )
        )
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][pml_cells[1][0]],
                    self.mesh_lines[2][-1],
                ),
            )
        )
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][-1 - pml_cells[1][1]],
                    self.mesh_lines[2][-1],
                ),
            )
        )
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][pml_cells[2][0]],
                ),
            )
        )
        boxes.append(
            Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][-1],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][-1 - pml_cells[2][1]],
                ),
            )
        )

        for box in boxes:
            box.set_increasing()

        return tuple(boxes)

    def add_line_manual(self, dim: int, pos: float) -> None:
        """
        Can be used to manually add a line to the mesh.  This should
        only be used for debugging purposes.  If the generated mesh is
        suboptimal, file a bug report.
        """
        self._add_lines_to_mesh([pos], dim)
        self._set_mesh_from_lines()
        self.sim.post_mesh()

    def sim_box(self, include_pml: bool = True) -> Box3:
        """
        """
        pml_cells = self.sim.boundary_conditions.pml_bounds()
        if include_pml:
            return Box3(
                Coordinate3(
                    self.mesh_lines[0][0],
                    self.mesh_lines[1][0],
                    self.mesh_lines[2][0],
                ),
                Coordinate3(
                    self.mesh_lines[0][-1],
                    self.mesh_lines[1][-1],
                    self.mesh_lines[2][-1],
                ),
            )
        return Box3(
            Coordinate3(
                self.mesh_lines[0][pml_cells[0][0]],
                self.mesh_lines[1][pml_cells[1][0]],
                self.mesh_lines[2][pml_cells[2][0]],
            ),
            Coordinate3(
                self.mesh_lines[0][-1 - pml_cells[0][0]],
                self.mesh_lines[1][-1 - pml_cells[1][0]],
                self.mesh_lines[2][-1 - pml_cells[2][0]],
            ),
        )

    def _gen_mesh_for_bounded_types(
        self, bounded_types: List[List[BoundedType]]
    ) -> None:
        """
        """
        for dim, btypes in enumerate(bounded_types):
            for btype in btypes:
                lower = btype.get_bounds()[0]
                upper = btype.get_bounds()[1]
                is_metal = btype.get_type() == Type.metal
                _, line_below = self._line_below(dim, lower)
                _, line_above = self._line_above(dim, upper)
                self._gen_mesh_in_bounds(
                    dim, lower, upper, line_below, line_above, is_metal
                )
                self._add_to_ranges_meshed(dim, lower, upper)

    def _add_to_ranges_meshed(
        self, dim: int, lower: float, upper: float
    ) -> None:
        """
        """
        self.ranges_meshed[dim].append([lower, upper])
        # TODO
        # self._consolidate_meshed_ranges(dim)

    def _consolidate_meshed_ranges(self, dim):
        """
        Order meshed ranges and consolidate contiguous ranges.
        """
        self.ranges_meshed[dim] = sorted(self.ranges_meshed[dim])
        self._range_union(dim)

    def _range_union(self, dim: int, start_idx: int = 0):
        """
        """
        while start_idx + 1 <= len(self.ranges_meshed[dim]) - 1:
            if (
                self.ranges_meshed[dim][start_idx][1]
                >= self.ranges_meshed[dim][start_idx + 1][0]
            ):
                self.ranges_meshed[dim].append(
                    [
                        self.ranges_meshed[dim][start_idx][0],
                        self.ranges_meshed[dim][start_idx + 1][1],
                    ]
                )
                del self.ranges_meshed[dim][start_idx : start_idx + 2]
            else:
                start_idx += 1

    def _type_above(self, dim: int, upper: float) -> Type:
        """
        """
        for btype in self.bounded_types[dim]:
            if btype.get_bounds()[0] == upper:
                return btype.get_type()

    def _type_above_meshed(self, dim: int, upper: float) -> bool:
        """
        """
        for btype in self.bounded_types[dim]:
            if btype.get_bounds()[0] == upper and btype.size() != 0:
                return self._pos_meshed(dim, btype.get_midpoint())

    def _type_below(self, dim: int, lower: float) -> Type:
        """
        """
        for btype in self.bounded_types[dim]:
            if btype.get_bounds()[1] == lower:
                return btype.get_type()

    def _type_below_meshed(self, dim: int, lower: float) -> bool:
        """
        """
        for btype in self.bounded_types[dim]:
            if btype.get_bounds()[1] == lower and btype.size() != 0:
                return self._pos_meshed(dim, btype.get_midpoint())

    def nearest_mesh_line(self, dim: int, pos: float) -> (int, float):
        """
        Find the nearest mesh line to a desired position for a given
        dimension.

        :param dim: 0, 1, or 2 for x, y, z.
        :param pos: desired position.

        :returns: (index, position) where index is the array index and
                  position is the actual dimension value.  If there
                  are no mesh lines for the dimension, return (None,
                  None)
        """
        lines = self.mesh_lines[dim]
        if not lines:
            return (None, None)
        bisect_pos = bisect_left(self.mesh_lines[dim], pos)
        if bisect_pos == 0:
            return (0, lines[0])
        elif bisect_pos == len(lines):
            return (bisect_pos - 1, lines[bisect_pos - 1])
        else:
            lower = lines[bisect_pos - 1]
            upper = lines[bisect_pos]
            if pos - lower < upper - pos:
                return (bisect_pos - 1, lower)
            else:
                return (bisect_pos, upper)

    def get_mesh_line(self, dim: int, index: int) -> float:
        """
        Get the mesh line position for a given dimension and index.
        Raises an error if the dimension or index are invalid.

        :param dim: 0, 1, or 2 for x, y, z.
        :param index: Line index.
        """
        if dim > 2 or dim < 0:
            raise ValueError("Invalid dimension provided.")
        if not self._mesh_valid_index(dim, index):
            raise ValueError("Mesh line index is outside valid range.")

        return self.mesh_lines[dim][index]

    def set_lines_equidistant(self, dim: int, lower: int, upper: int):
        """
        Make mesh lines equidistant from one another.  This should
        only be used when absolutely necessary.  I.e. when setting
        probes.  Will raise an error if lines cannot be moved.

        :param dim: 0, 1, or 2 for x, y, z.
        :param lower: Index of the first line.
        :param upper: Index of the last line.  This line is included.
        """
        if upper - lower == 1:
            warn("More than 2 lines should be specified.")
        for i in range(lower, upper + 1):
            if self.get_mesh_line(dim, i) in self.fixed_lines[dim]:
                raise RuntimeError("Trying to move an unmovable line.")

        lower_spacing = None
        if lower != 0:
            lower_spacing = self.get_mesh_line(
                dim, lower
            ) - self.get_mesh_line(dim, lower - 1)

        upper_spacing = None
        if upper != len(self.mesh_lines[dim]) - 1:
            upper_spacing = self.get_mesh_line(
                dim, upper + 1
            ) - self.get_mesh_line(dim, upper)

        num_spaces = upper - lower
        lower_pos = self.get_mesh_line(dim, lower)
        upper_pos = self.get_mesh_line(dim, upper)
        spacing = (upper_pos - lower_pos) / num_spaces

        if (
            lower_spacing and abs(lower_spacing - spacing) > self.smooth[dim]
        ) or (
            upper_spacing and abs(upper_spacing - spacing) > self.smooth[dim]
        ):
            raise RuntimeError(
                "Can't set equidistant lines and keep smoothness."
            )

        self._clear_mesh_in_bounds(lower_pos, upper_pos, dim)
        new_lines = np.linspace(lower_pos, upper_pos, num_spaces + 1)
        [self._add_mesh_line(dim, new_line) for new_line in new_lines]
        self._set_mesh_from_lines()

    def _clear_mesh_in_bounds(self, lower, upper, dim):
        """
        :param lower: Lower position.
        :param upper: Upper position.
        :param dim: is the dimension: 0, 1, 2 for x, y, or z.
        """
        lower_idx, _ = self._line_above_inc(dim, lower)
        upper_idx, _ = self._line_below_inc(dim, upper)
        upper_idx += 1
        del self.mesh_lines[dim][lower_idx:upper_idx]

    def _set_mesh_from_lines(self):
        """
        Generates the actual CSX mesh structure from mesh_lines.  This
        clears any preexisting mesh.
        """
        for i in range(3):
            self.mesh.ClearLines(i)
        for dim in range(3):
            for line in self.mesh_lines[dim]:
                add_line(grid=self.mesh, dim=dim, val=line)

    def _line_below(self, dim: int, pos: float) -> Tuple[int, float]:
        """
        Return the index and position of the nearest line below the
        provided one.
        """
        (idx, act_pos) = self.nearest_mesh_line(dim, pos)
        if act_pos is None:
            return (None, None)

        if fp_equalp(act_pos, pos):
            idx -= 1
            if self._mesh_valid_index(dim, idx):
                act_pos = self.get_mesh_line(dim, idx)

        if fp_ltp(act_pos, pos):
            return (idx, act_pos)

        idx -= 1
        if self._mesh_valid_index(dim, idx):
            act_pos = self.get_mesh_line(dim, idx)
            return (idx, act_pos)

        return (None, None)

    def _line_below_inc(self, dim: int, pos: float) -> Tuple[int, float]:
        """
        Return the index and position of the nearest line at or below
        the provided one.  This differs from ``_line_below`` in that
        ``_line_below`` is not inclusive.
        """
        (idx, act_pos) = self.nearest_mesh_line(dim, pos)
        if act_pos is None:
            return (None, None)

        if fp_lep(act_pos, pos):
            return (idx, act_pos)

        idx -= 1
        if self._mesh_valid_index(dim, idx):
            act_pos = self.get_mesh_line(dim, idx)
            return (idx, act_pos)

        return (None, None)

    def _line_above(self, dim: int, pos: float) -> Tuple[int, float]:
        """
        Return the index and position of the nearest line above the
        provided one.
        """
        (idx, act_pos) = self.nearest_mesh_line(dim, pos)
        if act_pos is None:
            return (None, None)

        if fp_equalp(act_pos, pos):
            idx += 1
            if self._mesh_valid_index(dim, idx):
                act_pos = self.get_mesh_line(dim, idx)

        if fp_gtp(act_pos, pos):
            return (idx, act_pos)

        idx += 1
        if self._mesh_valid_index(dim, idx):
            act_pos = self.get_mesh_line(dim, idx)
            return (idx, act_pos)

        return (None, None)

    def _line_above_inc(self, dim: int, pos: float) -> Tuple[int, float]:
        """
        Return the index and position of the nearest line at or above
        the provided one.  This differs from ``_line_above`` in that
        ``_line_above`` is not inclusive.
        """
        (idx, act_pos) = self.nearest_mesh_line(dim, pos)
        if act_pos is None:
            return (None, None)

        if fp_gep(act_pos, pos):
            return (idx, act_pos)

        idx += 1
        if self._mesh_valid_index(dim, idx):
            act_pos = self.get_mesh_line(dim, idx)
            return (idx, act_pos)

        return (None, None)

    def _mesh_valid_index(self, dim: int, index: int) -> bool:
        """
        Indicate whether index is valid for mesh lines.
        """
        if index >= 0 and index < len(self.mesh_lines[dim]):
            return True
        return False

    def _min_spacing(self, dist: float) -> float:
        """
        """
        return dist / (self.min_lines - 1)

    def _lower_spacing(
        self,
        dim: int,
        lower: float,
        line_below: float,
        dist: float,
        is_metal: bool,
    ) -> float:
        """
        Compute spacing at the lower boundary for a bounded type.
        """
        if is_metal:
            lower_spacing = self.metal_res
        else:
            lower_spacing = self.nonmetal_res

        lower_spacing = np.min([lower_spacing, self._min_spacing(dist)])

        if line_below and self._type_below_meshed(dim, lower):
            factor = 1
            if self._is_metal_bound(dim, lower) and not self._is_fixed_line(
                dim, lower
            ):
                if self._type_below(dim, lower) == Type.nonmetal:
                    factor = 3 / 2
                else:
                    factor = 3
            spacing = factor * (lower - line_below)
            lower_spacing = np.min([lower_spacing, spacing])

        return lower_spacing

    def _upper_spacing(
        self,
        dim: int,
        upper: float,
        line_above: float,
        dist: float,
        is_metal: bool,
    ) -> float:
        """
        Compute spacing at the upper boundary for a bounded type.
        """
        if is_metal:
            upper_spacing = self.metal_res
        else:
            upper_spacing = self.nonmetal_res

        upper_spacing = np.min([upper_spacing, self._min_spacing(dist)])

        if line_above and self._type_above_meshed(dim, upper):
            factor = 1
            if self._is_metal_bound(dim, upper) and not self._is_fixed_line(
                dim, upper
            ):
                if self._type_above(dim, upper) == Type.nonmetal:
                    factor = 3 / 2
                else:
                    factor = 3
            spacing = factor * (line_above - upper)
            upper_spacing = np.min([upper_spacing, spacing])

        return upper_spacing

    def _gen_mesh_in_bounds(
        self,
        dim: int,
        lower: float,
        upper: float,
        line_below: float,
        line_above: float,
        is_metal: bool,
    ) -> None:
        """
        Generate mesh lines for the given dimension and in the given
        bounds.

        :param dim: 0, 1, or 2 for x, y, z.
        :param lower: Lower bound position.
        :param upper: Upper bound position.
        :param line_below: Position of the nearest mesh line below the
            lower bound.  Set to None if none exists.
        :param line_above: Position of the nearest mesh line above the
            upper bound.  Set to None if none exists.
        :param is_metal: True if the current mesh is being generated
            for a metal structure.
        """
        # since we mesh smaller structures first, we only need to
        # worry about the case where the spacing is small. If its
        # large we assume we haven't meshed the adjacent structure and
        # ignore it.
        dist = upper - lower
        lower_spacing = self._lower_spacing(
            dim, lower, line_below, dist, is_metal
        )
        upper_spacing = self._upper_spacing(
            dim, upper, line_above, dist, is_metal
        )
        if is_metal:
            max_spacing = self.metal_res
        else:
            max_spacing = self.nonmetal_res

        if lower == upper:
            self._add_lines_to_mesh([lower], dim)
        else:
            # TODO why do we need to generate these lines first and
            # then use first_spacing and last_spacing instead of
            # lower_spacing and upper_spacing?
            lines = self._gen_lines_in_bounds(
                lower, upper, lower_spacing, upper_spacing, max_spacing, dim
            )

            if is_metal:
                first_spacing = lines[1] - lines[0]
                last_spacing = lines[-1] - lines[-2]
                # If there is already a line at the boundary (due to a
                # planar metal) there is no need to shift the line
                # inside the metal region. Moreover, doing so can
                # create a small spacing adjacent to a much larger
                # spacing, which violates smoothness.
                if not fp_equalp(
                    lower, self.sim_bounds[dim][0]
                ) and not self._is_fixed_line(dim, lower):
                    if (
                        self._pos_meshed(dim, lower)
                        and self._type_below(dim, lower) == Type.metal
                    ):
                        adj = 2 * first_spacing / 3
                    else:
                        adj = first_spacing / 3
                    lower += adj
                    # lower_spacing += adj
                if not fp_equalp(
                    upper, self.sim_bounds[dim][1]
                ) and not self._is_fixed_line(dim, upper):
                    if (
                        self._pos_meshed(dim, upper)
                        and self._type_above(dim, upper) == Type.metal
                    ):
                        adj = 2 * last_spacing / 3
                    else:
                        adj = last_spacing / 3
                    upper -= adj
                    # upper_spacing += adj
                # TODO is this good enough?
                lines = self._gen_lines_in_bounds(
                    lower,
                    upper,
                    lower_spacing,
                    upper_spacing,
                    max_spacing,
                    dim,
                )
            else:
                rebuild_lines = False
                if self._is_metal_bound(dim, lower):
                    rebuild_lines = True
                    first_spacing = lines[1] - lines[0]
                    # The metal side will use metal_res so we must use
                    # that too.
                    spacing = np.min([first_spacing, self.metal_res])
                    lower += 2 * spacing / 3
                if self._is_metal_bound(dim, upper):
                    rebuild_lines = True
                    last_spacing = lines[-1] - lines[-2]
                    spacing = np.min([last_spacing, self.metal_res])
                    upper -= 2 * spacing / 3
                if rebuild_lines:
                    lines = self._gen_lines_in_bounds(
                        lower,
                        upper,
                        lower_spacing,
                        upper_spacing,
                        max_spacing,
                        dim,
                    )

            self._add_lines_to_mesh(lines, dim)

    def _is_fixed_line(self, dim: int, pos: float) -> bool:
        """
        """
        return pos in self.fixed_lines[dim]

    def _is_metal_bound(self, dim: int, pos: float) -> bool:
        """
        """
        return pos in self.metal_bounds[dim]

    def _pos_meshed(self, dim: int, pos: float) -> bool:
        """
        Return whether a position has already been meshed.
        """
        meshed_ranges = self.ranges_meshed[dim]
        for rng in meshed_ranges:
            if _pos_in_bounds(pos, rng[0], rng[1]):
                return True

        return False

    def _gen_lines_in_bounds(
        self,
        lower: float,
        upper: float,
        lower_spacing: float,
        upper_spacing: float,
        max_spacing: float,
        dim: int,
    ) -> np.array:
        """
        Generate a list of lines within specified boundary positions
        for a provided dimension.

        :param lower: Lower boundary position.
        :param upper: Upper boundary position.
        :param lower_spacing: Spacing between lines adjacent to the
            lower boundary.
        :param upper_spacing: Spacing between lines adjacent to the
            upper boundary.
        :param dim: Dimension. 0, 1, or 2 for x, y, or z.
        """
        dist = upper - lower
        smaller_spacing = np.min([lower_spacing, upper_spacing])
        larger_spacing = np.max([lower_spacing, upper_spacing])
        num_lower = dist / larger_spacing

        # When the spacing in the current region must be smaller than
        # the spacings imposed by the boundaries, or the spacing in
        # the region will not be able to increase enough from the
        # smaller spacing to the larger spacing, compute the lines for
        # the whole region directly. The first case happens when the
        # minimum number of lines constrains the spacing.
        if (
            num_lower < self.min_lines
            or _spacing_at_dist(smaller_spacing, dist, self.smooth[dim])
            < larger_spacing
        ):
            return _lines_const_factor_in_bounds(
                lower,
                upper,
                lower_spacing,
                upper_spacing,
                dim,
                self.min_lines,
                self.smooth[dim],
            )

        # Break the region up into two subregions based on a "midpoint"
        # computed where the spacings can be the same (see
        # ``_dist_for_max_spacings``). We also handle the case where
        # the spacings at each boundary are the same here rather than
        # as a single region in the previous conditional. The reason
        # is that it is generally possible to increase and then
        # decrease the spacing such that the boundary spacings are
        # still satisfied, but fewer mesh lines are used in the
        # region. Visually: | | |  |   |   |  | | |
        mid_spacing_dist = _dist_for_max_spacings(
            lower_spacing, upper_spacing, dist, self.smooth[dim]
        )
        midpt = lower + mid_spacing_dist
        lower_factor, lower_num = _num_for_factor(
            self.smooth[dim], lower_spacing, midpt - lower
        )
        upper_factor, upper_num = _num_for_factor(
            self.smooth[dim], upper_spacing, upper - midpt
        )
        mid_spacing = np.min(
            [
                max_spacing,
                lower_spacing * (lower_factor ** lower_num),
                upper_spacing * (upper_factor ** upper_num),
            ]
        )

        while lower_num + upper_num < self.min_lines:
            lower_num += 1
            upper_num += 1

        lines_lower = _lines_const_factor_in_bounds(
            lower,
            midpt,
            lower_spacing,
            mid_spacing,
            dim,
            lower_num,
            self.smooth[dim],
        )
        lines_upper = _lines_const_factor_in_bounds(
            midpt,
            upper,
            mid_spacing,
            upper_spacing,
            dim,
            upper_num,
            self.smooth[dim],
        )
        lines = np.concatenate([lines_lower, lines_upper])

        return _remove_dups(lines, self.fixed_lines[dim])

    def _add_lines_to_mesh(self, lines: np.array, dim: int) -> None:
        """
        Add an array of lines to the mesh for a given dimension.  This
        preserves the sorted order of lines and ensures no duplicate
        lines.
        """
        for line in lines:
            self._add_mesh_line(dim, line)

        self.mesh_lines[dim] = _remove_dups(
            self.mesh_lines[dim], self.fixed_lines[dim]
        )

    def _add_mesh_line(self, dim: int, pos: float) -> None:
        """
        Add a line to the mesh.  Preserves sorted order of mesh lines.
        This should only ever be called from _add_lines_to_mesh, since
        this will not remove duplicate lines.

        :param dim: 0, 1, or 2 for x, y, z.
        :param pos: New line position.
        """
        insort_left(self.mesh_lines[dim], pos)

    def _metal_bound_delta(
        self,
        lower: float,
        upper: float,
        lower_spacing: float,
        upper_spacing: float,
    ) -> List[float]:
        """
        The amount by which to move the bounds of a metal inside the
        metal.

        :param lower: Actual lower position of the metal.
        :param upper: Actual upper position of the metal.
        :param lower_spacing: Spacing to line below the metal.
        :param upper_spacing: Spacing to line above the metal.

        :returns: List of two elements corresponding to the amount to
                  adjust the lower and upper bounds, respectively.
        """

    def _update_smallest_res(self, new_res: float) -> None:
        """
        Update smallest recorded resolution if new resolution is
        smaller than current.
        """
        self.smallest_res = np.min([self.smallest_res, new_res])

    def add_fixed_line(self, dim: int, pos: float) -> None:
        """
        """
        self.fixed_lines[dim].append(pos)
        self.fixed_lines[dim].sort()

    def _set_fixed_lines(self, prims: List[CSPrimitives]) -> None:
        """
        """
        for prim in prims:
            prim_bounds = _get_prim_bounds(prim)
            for dim in range(3):
                if fp_equalp(prim_bounds[dim][0], prim_bounds[dim][1]):
                    # TODO is rounding here necessary?
                    self.add_fixed_line(dim, fp_nearest(prim_bounds[dim][0]))

                self.fixed_lines[dim].sort()
                self.fixed_lines[dim] = _remove_dups(self.fixed_lines[dim])

    def _bounded_types(
        self, bounds: List[List[float]], prims: List[CSPrimitives]
    ) -> List[List[BoundedType]]:
        """
        """
        bounded_types = [[], [], []]
        for dim, dim_bounds in enumerate(bounds):
            last_bound = None
            for bound in dim_bounds:
                if bound in self.fixed_lines[dim]:
                    if last_bound is not None:
                        mid_pos = np.average([last_bound, bound])
                        prop_type = _type_at_pos(prims, dim, mid_pos)
                        btype = BoundedType(prop_type, last_bound, bound)
                        bounded_types[dim].append(btype)
                    prop_type = _type_at_pos(prims, dim, bound)
                    btype = BoundedType(prop_type, bound, bound)
                    bounded_types[dim].append(btype)
                elif last_bound is not None:
                    mid_pos = np.average([last_bound, bound])
                    prop_type = _type_at_pos(prims, dim, mid_pos)
                    btype = BoundedType(prop_type, last_bound, bound)
                    bounded_types[dim].append(btype)

                last_bound = bound

        return bounded_types

    def _set_expanded_bounds(
        self, bounded_types: List[List[BoundedType]]
    ) -> List[List[BoundedType]]:
        """
        Add bounded types based on simulation_bounds and expand_bounds
        passed to generate_mesh.

        :param bounded_types: A list of lists of BoundedType, where
            the BoundedTypes are given in order of their lower and
            upper bounds.
        """
        if self.simulation_bounds:
            for dim, bounds in enumerate(self.simulation_bounds):
                existing_lower = bounded_types[dim][0].get_bounds()[0]
                existing_upper = bounded_types[dim][-1].get_bounds()[1]
                if fp_gtp(bounds[0], existing_lower) or fp_ltp(
                    bounds[1], existing_upper
                ):
                    raise ValueError(
                        "Requested simulation bounds that would ignore part "
                        "of a physical structure."
                    )
                else:
                    if not fp_equalp(bounds[0], existing_lower):
                        btype = BoundedType(
                            Type.air, bounds[0], existing_lower
                        )
                        bounded_types[dim].insert(0, btype)
                    if not fp_equalp(bounds[1], existing_upper):
                        btype = BoundedType(
                            Type.air, existing_upper, bounds[1]
                        )
                        bounded_types[dim].append(btype)
        else:
            for dim in range(3):
                existing_lower = bounded_types[dim][0].get_bounds()[0]
                existing_upper = bounded_types[dim][-1].get_bounds()[1]
                expand_lower = self.expand_bounds[dim][0]
                expand_upper = self.expand_bounds[dim][-1]
                if expand_lower != 0:
                    new_low = existing_lower - (
                        self.nonmetal_res * expand_lower
                    )
                    btype = BoundedType(Type.air, new_low, existing_lower)
                    bounded_types[dim].insert(0, btype)
                if expand_upper != 0:
                    new_high = existing_upper + (
                        self.nonmetal_res * expand_upper
                    )
                    btype = BoundedType(Type.air, existing_upper, new_high)
                    bounded_types[dim].append(btype)

        for dim in range(3):
            self.sim_bounds[dim] = [
                bounded_types[dim][0].get_bounds()[0],
                bounded_types[dim][-1].get_bounds()[1],
            ]

        return bounded_types

    def _set_metal_bounds(
        self, bounded_types: List[List[BoundedType]]
    ) -> None:
        """
        Set the metal boundaries based on the bounded types.  Ignore
        zero-length metals, which aren't metal bounds which require
        adherance to the thirds rule.
        """
        for dim, btypes in enumerate(bounded_types):
            for btype in btypes:
                if btype.get_type() == Type.metal:
                    bounds = btype.get_bounds()
                    # Ignore zero-length metals.
                    if not fp_equalp(bounds[0], bounds[1]):
                        self._add_metal_bound(dim, bounds[0])
                        self._add_metal_bound(dim, bounds[1])

            self.metal_bounds[dim] = _remove_dups(
                self.metal_bounds[dim], self.fixed_lines[dim]
            )

    def _add_metal_bound(self, dim: int, pos: float) -> None:
        """
        Add a pos to metal_bounds.  Preserves sorted line order.

        :param dim: 0, 1, or 2 for x, y, z.
        :param pos: New line position.
        """
        insort_left(self.metal_bounds[dim], pos)
