# This file is part of CO𝘕CEPT, the cosmological 𝘕-body code in Python.
# Copyright © 2015–2019 Jeppe Mosgaard Dakin.
#
# CO𝘕CEPT is free software: You can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CO𝘕CEPT is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CO𝘕CEPT. If not, see http://www.gnu.org/licenses/
#
# The author of CO𝘕CEPT can be contacted at dakin(at)phys.au.dk
# The latest version of CO𝘕CEPT is available at
# https://github.com/jmd-dk/concept/



# Import everything from the commons module.
# In the .pyx file, Cython declared variables will also get cimported.
from commons import *

# Cython imports
cimport('from communication import '
    'communicate_ghosts, get_buffer, sendrecv_component, rank_neighbouring_domain, '
    'domain_subdivisions, '
)
cimport('from communication import domain_size_x , domain_size_y , domain_size_z' )
cimport('from communication import domain_start_x, domain_start_y, domain_start_z')
cimport('from ewald import get_ewald_grid')
cimport('from mesh import diff_domaingrid, domain_decompose, fft, slab_decompose')
cimport(
    'from mesh import                         '
    '    get_deconvolution,                   '
    '    interpolate_components,              '
    '    interpolate_domaingrid_to_particles, '
    '    interpolate_grid_to_grid,            '
)
cimport('from species import tentatively_refine_subtiling, accept_or_reject_subtiling_refinement')
# Import interactions defined in other modules
cimport('from gravity import *')

# Function pointer types used in this module
pxd("""
ctypedef void (*func_interaction)(
    Component,        # receiver
    Component,        # supplier
    str,              # pairing_level
    Py_ssize_t[::1],  # tile_indices_receiver
    Py_ssize_t**,     # tile_indices_supplier_paired
    Py_ssize_t*,      # tile_indices_supplier_paired_N
    int,              # rank_supplier
    bint,             # only_supply
    dict,             # ᔑdt
    dict,             # interaction_extra_args
)
ctypedef double (*func_potential)(
    double,  # k2
)
""")



# Generic function implementing component-component pairing
@cython.header(
    # Arguments
    receivers=list,
    suppliers=list,
    interaction=func_interaction,
    ᔑdt=dict,
    dependent=list,
    affected=list,
    deterministic='bint',
    pairing_level=str,
    interaction_name=str,
    interaction_extra_args=dict,
    # Locals
    anticipate_refinement='bint',
    anticipation_period='Py_ssize_t',
    attempt_refinement='bint',
    component_pair=set,
    computation_time='double',
    judge_refinement='bint',
    judgement_period='Py_ssize_t',
    lowest_active_rung='signed char',
    only_supply='bint',
    pairings=list,
    receiver='Component',
    refinement_offset='Py_ssize_t',
    refinement_period='Py_ssize_t',
    rung_index='signed char',
    subtiles_computation_times_N_interaction='Py_ssize_t[::1]',
    subtiles_computation_times_interaction='double[::1]',
    subtiles_computation_times_sq_interaction='double[::1]',
    subtiling='Tiling',
    subtiling_name=str,
    subtiling_name_2=str,
    supplier='Component',
    tile_sorted=set,
    tiling_name=str,
    returns='void',
)
def component_component(
    receivers, suppliers, interaction, ᔑdt, dependent, affected,
    deterministic, pairing_level, interaction_name, interaction_extra_args={},
):
    """This function takes care of pairings between all receiver and
    supplier components. It then calls doman_domain.
    """
    # The names used to refer to the domain and tile level tiling
    # (tiles and subtiles). In the case of pairing_level == 'domain',
    # no actual tiling will take place, but we still need the
    # tile + subtile structure. For this, the trivial tiling,
    # spanning the box, is used.
    if 𝔹[pairing_level == 'tile']:
        tiling_name      = f'{interaction_name} (tiles)'
        subtiling_name   = f'{interaction_name} (subtiles)'
        subtiling_name_2 = f'{interaction_name} (subtiles 2)'
    else:  # pairing_level == 'domain':
        tiling_name = subtiling_name = 'trivial'
    # Set flags anticipate_refinement, attempt_refinement and
    # judge_refinement. The first signals whether a tentative subtiling
    # refinement attempt is comming up soon, in which case we should
    # be collecting computation time data of the current subtiling.
    # The second specifies whether a tentative refinement of the
    # subtilings in use should be performed now, meaning prior to
    # the interaction. The third specifies whether the previously
    # performed tentative refinement should be concluded, resulting in
    # either accepting or rejecting the refinement.
    anticipate_refinement = attempt_refinement = judge_refinement = False
    if 𝔹[pairing_level == 'tile'
        and shortrange_params[interaction_name]['subtiling'][0] == 'automatic'
    ]:
        # The anticipation_period and judgement_period specifies the
        # number of time steps spend collecting computation time data
        # before and after a tentative sutiling refinement.
        # The refinement will be judged after the first interaction of
        # the time step after judgement_period time steps has gone by
        # after the tentative refinement (there may be many more
        # interactions in this time step, depending on N_rungs).
        # Note that changes to anticipation_period or judgement_period
        # need to be reflected in the subtiling_refinement_period_min
        # variable, defined in the commons module. The relation is
        # subtiling_refinement_period_min = (
        #     anticipation_period + judgement_period + 1)
        anticipation_period = 4
        judgement_period = 2
        subtiles_computation_times_interaction    = subtiles_computation_times   [interaction_name]
        subtiles_computation_times_sq_interaction = subtiles_computation_times_sq[interaction_name]
        subtiles_computation_times_N_interaction  = subtiles_computation_times_N [interaction_name]
        for receiver in receivers:
            subtiling = receiver.tilings.get(subtiling_name)
            if subtiling is None:
                continue
            refinement_period = subtiling.refinement_period
            refinement_offset = subtiling.refinement_offset
            if refinement_period == 0:
                abort(
                    f'The subtiling "{subtiling_name}" is set to use automatic subtiling '
                    f'refinement, but it has a refinement period of {refinement_period}.'
                )
            # We judge the attempted refinement after 2 whole time steps
            # has gone by; this one and the next. The refinement will
            # then be judged after the first interaction on the third
            # time step (there may be many more interactions
            # if N_rungs > 1).
            if interaction_name in subtilings_under_tentative_refinement:
                anticipate_refinement = True
                judge_refinement = (
                    ℤ[universals.time_step + refinement_offset + 1] % refinement_period == 0
                )
            else:
                attempt_refinement = (
                    (ℤ[universals.time_step + refinement_offset + 1] + judgement_period
                        ) % refinement_period == 0
                )
                # We begin storing the computation time data of the
                # current subtiling 4 time steps before we tentatively
                # apply the new subtiling.
                anticipate_refinement = (
                    (ℤ[universals.time_step + refinement_offset + 1] + judgement_period
                        ) % refinement_period >= refinement_period - anticipation_period
                )
            break
    # Do the tentative subtiling refinement, if required
    if attempt_refinement:
        # Copy the old computation times to new locations in
        # subtiles_computation_times_interaction, making room for the
        # new computation times.
        for rung_index in range(N_rungs):
            subtiles_computation_times_interaction[N_rungs + rung_index] = (
                subtiles_computation_times_interaction[rung_index]
            )
            subtiles_computation_times_sq_interaction[N_rungs + rung_index] = (
                subtiles_computation_times_sq_interaction[rung_index]
            )
            subtiles_computation_times_N_interaction[N_rungs + rung_index] = (
                subtiles_computation_times_N_interaction[rung_index]
            )
            subtiles_computation_times_interaction   [rung_index] = 0
            subtiles_computation_times_sq_interaction[rung_index] = 0
            subtiles_computation_times_N_interaction [rung_index] = 0
        # Replace the subtilings with slighly refined versions
        subtilings_under_tentative_refinement.add(interaction_name)
        tentatively_refine_subtiling(interaction_name)
    # Pair each receiver with all suppliers and let them interact
    pairings = []
    tile_sorted = set()
    computation_time = 0  # Total tile-tile computation time for this call to component_component()
    for receiver in receivers:
        for supplier in suppliers:
            component_pair = {receiver, supplier}
            if component_pair in pairings:
                continue
            pairings.append(component_pair)
            # Make sure that the tile sorting of particles
            # in the two components are up-to-date.
            with unswitch(1):
                if receiver not in tile_sorted:
                    receiver.tile_sort(tiling_name)
                    tile_sorted.add(receiver)
                    # Also ensure existence of subtiling
                    receiver.init_tiling(subtiling_name)
            if supplier not in tile_sorted:
                supplier.tile_sort(tiling_name)
                tile_sorted.add(supplier)
                # Also ensure existence of subtiling
                supplier.init_tiling(subtiling_name)
            # Flag specifying whether the supplier should only supply
            # forces to the receiver and not receive any force itself.
            only_supply = (supplier not in receivers)
            # Pair up domains for the current
            # receiver and supplier component.
            domain_domain(
                receiver, supplier, interaction, ᔑdt, dependent, affected, only_supply,
                deterministic, pairing_level, interaction_name, interaction_extra_args,
            )
        # The interactions between the receiver and all suppliers are
        # now done. Add the accumulated computation time to the local
        # computation_time variable, then nullify the computation time
        # stored on the subtiling, so that it is ready for new data.
        # To keep the total computation time tallied up over the entire
        # time step present on the subtiling, add the currently stored
        # computation time to the computation_time_total attribute
        # before doing the nullification.
        subtiling = receiver.tilings[subtiling_name]
        computation_time += subtiling.computation_time
        subtiling.computation_time_total += subtiling.computation_time
        subtiling.computation_time = 0
    # All interactions are now done. If the measured computation time
    # should be used for automatic subtiling refinement, store this
    # outside of this function.
    if 𝔹[pairing_level == 'tile'
        and shortrange_params[interaction_name]['subtiling'][0] == 'automatic'
    ]:
        # The computation time depends drastically on which rungs are
        # currently active. We therefore store the total computation
        # time according to the current lowest active rung.
        if anticipate_refinement or attempt_refinement or judge_refinement:
            lowest_active_rung = ℤ[N_rungs - 1]
            for receiver in receivers:
                if receiver.lowest_active_rung < lowest_active_rung:
                    lowest_active_rung = receiver.lowest_active_rung
                    if lowest_active_rung == 0:
                        break
            subtiles_computation_times_interaction   [lowest_active_rung] += computation_time
            subtiles_computation_times_sq_interaction[lowest_active_rung] += computation_time**2
            subtiles_computation_times_N_interaction [lowest_active_rung] += 1
        # If it is time to judge a previously attempted refinement,
        # do so and reset the computation time.
        if judge_refinement:
            subtilings_under_tentative_refinement.remove(interaction_name)
            accept_or_reject_subtiling_refinement(
                interaction_name,
                subtiles_computation_times_interaction,
                subtiles_computation_times_sq_interaction,
                subtiles_computation_times_N_interaction,
            )
            subtiles_computation_times_interaction   [:] = 0
            subtiles_computation_times_sq_interaction[:] = 0
            subtiles_computation_times_N_interaction [:] = 0
# Containers used by the component_component() function.
# The subtiles_computation_times and subtiles_computation_times_N are
# used to store total computation times and numbers for performed
# interations. They are indexed as
# subtiles_computation_times[interaction_name][rung_index],
# resulting in the accumulated computation time for this interaction
# when the lowest active rung corresponds to rung_index.
# The subtilings_under_tentative_refinement set contain names of
# interactions the subtilings of which are currently under
# tentative refinement.
cython.declare(
    subtiles_computation_times=object,
    subtiles_computation_times_sq=object,
    subtiles_computation_times_N=object,
    subtilings_under_tentative_refinement=set,
)
subtiles_computation_times = collections.defaultdict(
    lambda: zeros(ℤ[2*N_rungs], dtype=C2np['double'])
)
subtiles_computation_times_sq = collections.defaultdict(
    lambda: zeros(ℤ[2*N_rungs], dtype=C2np['double'])
)
subtiles_computation_times_N = collections.defaultdict(
    lambda: zeros(ℤ[2*N_rungs], dtype=C2np['Py_ssize_t'])
)
subtilings_under_tentative_refinement = set()

# Generic function implementing domain-domain pairing
@cython.header(
    # Arguments
    receiver='Component',
    supplier='Component',
    interaction=func_interaction,
    ᔑdt=dict,
    dependent=list,
    affected=list,
    only_supply='bint',
    deterministic='bint',
    pairing_level=str,
    interaction_name=str,
    interaction_extra_args=dict,
    # Locals
    domain_pair_nr='Py_ssize_t',
    interact='bint',
    only_supply_passed='bint',
    rank_recv='int',
    rank_send='int',
    ranks_recv='int[::1]',
    ranks_send='int[::1]',
    supplier_extrl='Component',
    supplier_local='Component',
    tile_indices='Py_ssize_t[:, ::1]',
    tile_indices_receiver='Py_ssize_t[::1]',
    tile_indices_supplier='Py_ssize_t[::1]',
    tile_indices_supplier_paired='Py_ssize_t**',
    tile_indices_supplier_paired_N='Py_ssize_t*',
    tile_pairings_index='Py_ssize_t',
    returns='void',
)
def domain_domain(
    receiver, supplier, interaction, ᔑdt, dependent, affected,
    only_supply, deterministic, pairing_level, interaction_name, interaction_extra_args,
):
    """This function takes care of pairings between the domains
    containing particles/fluid elements of the passed receiver and
    supplier component.
    As the components are distributed at the domain level,
    all communication needed for the interaction will be taken care of
    by this function. The receiver will not be communicated, while the
    supplier will be sent to other processes (domains) and also received
    back from other processes. Thus both local and external versions of
    the supplier exist, called supplier_local and supplier_extrl.
    The dependent and affected arguments specify which attributes of the
    supplier and receiver component are needed to supply and receive
    the force, respectively. Only these attributes will be communicated.
    If affected is an empty list, this is not really an interaction.
    In this case, every domain will both send and receive from every
    other domain.
    """
    # Just to satisfy the compiler
    tile_indices_receiver = tile_indices_supplier = None
    # Get the process ranks to send to and receive from.
    # When only_supply is True, each domain will be paired with every
    # other domain, either in the entire box (pairing_level == 'domain')
    # or just among the neighbouring domains (pairing_level == 'tile').
    # When only_supply is False, the results of an interaction
    # computed on one process will be send back to the other
    # participating process and applied, cutting the number of domain
    # pairs roughly in half.
    ranks_send, ranks_recv = domain_domain_communication(pairing_level, only_supply)
    # Backup of the passed only_supply boolean
    only_supply_passed = only_supply
    # Pair this process/domain with whichever other
    # processes/domains are needed. This process is paired
    # with two other processes simultaneously. This process/rank sends
    # a copy of the local supplier (from now on referred to
    # as supplier_local) to rank_send, while receiving the external
    # supplier (supplier_extrl) from rank_recv.
    # On each process, the local receiver and the external
    # (received) supplier_extrl then interact.
    supplier_local = supplier
    for domain_pair_nr in range(ranks_send.shape[0]):
        # Process ranks to send to and receive from
        rank_send = ranks_send[domain_pair_nr]
        rank_recv = ranks_recv[domain_pair_nr]
        # The passed interaction function should always update the
        # particles of the receiver component within the local domain,
        # due to the particles of the external supplier component,
        # within whatever domain they happen to be in.
        # Unless the supplier component is truly only a supplier and
        # not also a receiver (only_supply is True), the particles
        # that supply the force also need to be updated by the passed
        # interaction function. It is important that the passed
        # interaction function do not update the affected variables
        # directly (e.g. mom for gravity), but instead update the
        # corresponding buffers (e.g. Δmom for gravity). These are the
        # buffers that will be communicated, but just as importantly,
        # Δmom is used to figure out which short-range rung any given
        # particle belongs to.
        # Special cases described below may change whether or not the
        # interaction between this particular domain pair should be
        # carried out on the local process (specified by the
        # interact flag), or whether the only_supply
        # flag should be changed.
        interact = True
        only_supply = only_supply_passed
        with unswitch:
            if 𝔹[pairing_level == 'domain' and not only_supply_passed]:
                if rank_send == rank_recv != rank:
                    # We are dealing with the special case where the
                    # local process and some other (with a rank given by
                    # rank_send == rank_recv) both send all of their
                    # particles to each other, after which the exact
                    # same interaction takes place on both processes.
                    # In such a case, even when only_supply is False,
                    # there is no need to communicate the interaction
                    # results, as these are already known to both
                    # processes. Thus, we always pass in only_supply as
                    # being True in such cases.
                    only_supply = True
                    # In the case of a non-deterministic interaction,
                    # the above logic no longer holds, as the two
                    # versions of the supposedly same interaction
                    # computed on different processes will not be
                    # identical. In such cases, perform the interaction
                    # only on one of the two processes. The process with
                    # the lower rank is chosen for the job.
                    with unswitch:
                        if not deterministic:
                            interact = (rank < rank_send)
                            only_supply = False
        # Communicate the dependent variables (e.g. pos for gravity) of
        # the supplier. For pairing_level == 'domain', communicate all
        # local particles. For pairing_level == 'tile', we only need to
        # communicate particles within the tiles that are going to
        # interact during the current domain-domain pairing.
        with unswitch:
            if 𝔹[pairing_level == 'tile']:
                # Find interacting tiles
                tile_indices = domain_domain_tile_indices(
                    receiver, supplier_local, only_supply_passed, domain_pair_nr, interaction_name)
                tile_indices_receiver = tile_indices[0, :]
                tile_indices_supplier = tile_indices[1, :]
            else:  # pairing_level == 'domain'
                # For domain level pairing we make use of
                # the trivial tiling, containing a single tile.
                tile_indices_receiver = tile_indices_supplier = tile_indices_trivial
                tile_indices_supplier_paired = tile_indices_trivial_paired
                tile_indices_supplier_paired_N = tile_indices_trivial_paired_N
        supplier_extrl = sendrecv_component(
            supplier_local, dependent, pairing_level, interaction_name, tile_indices_supplier,
            dest=rank_send, source=rank_recv,
        )
        # Let the local receiver interact with the
        # external supplier_extrl. This will update the affected
        # variable buffers (e.g. Δmom for gravity) of the local
        # receiver, and of the external supplier if only_supply
        # is False.
        if interact:
            with unswitch:
                if 𝔹[pairing_level == 'tile']:
                    # Get the supplier tiles with which to pair each
                    # receiver tile and perform the interaction
                    # at the tile level.
                    tile_pairings_index = get_tile_pairings(
                        receiver, supplier, tile_indices_receiver, tile_indices_supplier,
                        rank_recv, only_supply_passed, domain_pair_nr, interaction_name,
                    )
                    tile_indices_supplier_paired   = tile_pairings_cache  [tile_pairings_index]
                    tile_indices_supplier_paired_N = tile_pairings_N_cache[tile_pairings_index]
                    interaction(
                        receiver, supplier_extrl, pairing_level,
                        tile_indices_receiver,
                        tile_indices_supplier_paired, tile_indices_supplier_paired_N,
                        rank_recv, only_supply, ᔑdt, interaction_extra_args,
                    )
                else:  # pairing_level == 'domain'
                    # Perform the interaction now, at the domain level
                    interaction(
                        receiver, supplier_extrl, pairing_level,
                        tile_indices_receiver,
                        tile_indices_supplier_paired, tile_indices_supplier_paired_N,
                        rank_recv, only_supply, ᔑdt, interaction_extra_args,
                    )
        # Send the populated buffers back to the process from which the
        # external supplier_extrl came. Add the received values in the
        # buffers to the affected variable buffers (e.g. Δmom for
        # gravity) of the local supplier_local. Note that we should not
        # do this in the case of a local interaction (rank_send == rank)
        # or in a case where only_supply is True.
        if rank_send != rank and not only_supply:
            sendrecv_component(
                supplier_extrl, affected, pairing_level, interaction_name, tile_indices_supplier,
                dest=rank_recv, source=rank_send, component_recv=supplier_local,
            )
            # Nullify the Δ buffers of the external supplier_extrl,
            # leaving this with no leftover junk.
            supplier_extrl.nullify_Δ(affected)
# Tile indices for the trivial tiling,
# used by the domain_domain function.
cython.declare(
    tile_indices_trivial='Py_ssize_t[::1]',
    tile_indices_trivial_paired='Py_ssize_t**',
    tile_indices_trivial_paired_N='Py_ssize_t*',
)
tile_indices_trivial = zeros(1, dtype=C2np['Py_ssize_t'])
tile_indices_trivial_paired = malloc(1*sizeof('Py_ssize_t*'))
tile_indices_trivial_paired[0] = cython.address(tile_indices_trivial[:])
tile_indices_trivial_paired_N = malloc(1*sizeof('Py_ssize_t'))
tile_indices_trivial_paired_N[0] = tile_indices_trivial.shape[0]

# Function returning the indices of the tiles of the local receiver and
# supplier which take part in tile-tile interactions under the
# domain-domain pairing with number domain_pair_nr.
@cython.header(
    # Arguments
    receiver='Component',
    supplier='Component',
    only_supply='bint',
    domain_pair_nr='Py_ssize_t',
    interaction_name=str,
    # Locals
    dim='int',
    domain_pair_offsets='Py_ssize_t[:, ::1]',
    domain_pair_offset='Py_ssize_t[::1]',
    sign='int',
    tile_indices='Py_ssize_t[:, ::1]',
    tile_indices_all=list,
    tile_indices_component='Py_ssize_t[::1]',
    tile_indices_list=list,
    tile_layout='Py_ssize_t[:, :, ::1]',
    tile_layout_slice_end='Py_ssize_t[::1]',
    tile_layout_slice_start='Py_ssize_t[::1]',
    tiling='Tiling',
    tiling_name=str,
    returns='Py_ssize_t[:, ::1]',
)
def domain_domain_tile_indices(receiver, supplier, only_supply, domain_pair_nr, interaction_name):
    tile_indices_all = domain_domain_tile_indices_dict.get((receiver, supplier, only_supply))
    if tile_indices_all is None:
        tile_indices_all = [None]*27
        domain_domain_tile_indices_dict[receiver, supplier, only_supply] = tile_indices_all
    else:
        tile_indices = tile_indices_all[domain_pair_nr]
        if tile_indices is not None:
            return tile_indices
    tile_layout_slice_start = empty(3, dtype=C2np['Py_ssize_t'])
    tile_layout_slice_end   = empty(3, dtype=C2np['Py_ssize_t'])
    domain_pair_offsets = domain_domain_communication_dict[
        'tile', only_supply, 'domain_pair_offsets']
    domain_pair_offset = domain_pair_offsets[domain_pair_nr, :]
    tile_indices_list = []
    tiling_name = f'{interaction_name} (tiles)'
    for i, component in enumerate((receiver, supplier)):
        tiling = component.tilings[tiling_name]
        tile_layout = tiling.layout
        sign = {0: -1, 1: +1}[i]
        for dim in range(3):
            if domain_pair_offset[dim] == -sign:
                tile_layout_slice_start[dim] = 0
                tile_layout_slice_end[dim]   = 1
            elif domain_pair_offset[dim] == 0:
                tile_layout_slice_start[dim] = 0
                tile_layout_slice_end[dim]   = tile_layout.shape[dim]
            elif domain_pair_offset[dim] == +sign:
                tile_layout_slice_start[dim] = tile_layout.shape[dim] - 1
                tile_layout_slice_end[dim]   = tile_layout.shape[dim]
        tile_indices_component = asarray(tile_layout[
            tile_layout_slice_start[0]:tile_layout_slice_end[0],
            tile_layout_slice_start[1]:tile_layout_slice_end[1],
            tile_layout_slice_start[2]:tile_layout_slice_end[2],
        ]).flatten()
        tile_indices_list.append(tile_indices_component)
    tile_indices = asarray(tile_indices_list, dtype=C2np['Py_ssize_t'])
    tile_indices_all[domain_pair_nr] = tile_indices
    return tile_indices
# Cached results of the domain_domain_tile_indices function
# are stored in the dict below.
cython.declare(domain_domain_tile_indices_dict=dict)
domain_domain_tile_indices_dict = {}

# Function returning the process ranks with which to pair
# the local process/domain in the domain_domain function,
# depending on the pairing level and supplier only supplies
# or also receives.
@cython.header(
    # Arguments
    pairing_level=str,
    only_supply='bint',
    # Locals
    i='Py_ssize_t',
    returns=tuple,
)
def domain_domain_communication(pairing_level, only_supply):
    ranks = domain_domain_communication_dict.get((pairing_level, only_supply))
    if ranks:
        return ranks
    if pairing_level == 'domain':
        # When only_supply is True, each process should be paired with
        # all processes. When only_supply is False, advantage is taken
        # of the fact that a process is paired with two other processes
        # simultaneously, meaning that the number of pairings is cut
        # (roughly) in half. The particular order implemented below
        # is of no importance.
        N_domain_pairs = nprocs if only_supply else 1 + nprocs//2
        ranks_send = empty(N_domain_pairs, dtype=C2np['int'])
        ranks_recv = empty(N_domain_pairs, dtype=C2np['int'])
        for i in range(N_domain_pairs):
            ranks_send[i] = mod(rank + i, nprocs)
            ranks_recv[i] = mod(rank - i, nprocs)
        domain_domain_communication_dict[pairing_level, only_supply] = (ranks_send, ranks_recv)
    elif pairing_level == 'tile':
        # When only_supply is True, each domian should be paired with
        # itself and all 26 neighbouring domains. Even though we might
        # have nprocs < 27, meaning that some of the neighbouring
        # domains might be the same, we always include all of them.
        # If only_supply is False, advantage is taken of the fact that a
        # domain is simultaneously paired with two other domains along
        # the same direction (e.g. to the left and to the right),
        # cutting the number of pairings (roughly) in half. The order is
        # as specified below, and stored (as directions, not ranks) in
        # domain_domain_communication_dict[
        #     'tile', only_supply, 'domain_pair_offsets'].
        ranks_send = []
        ranks_recv = []
        offsets_list = []
        # - This domain itself
        offsets = np.array([0, 0, 0], dtype=C2np['int'])
        offsets_list.append(offsets.copy())
        ranks_send.append(rank_neighbouring_domain(*(+offsets)))
        ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
        # - Domains at the 6 faces
        #   (when only_supply is False, send right, forward, upward)
        direction = np.array([+1, 0, 0], dtype=C2np['int'])
        for i in range(3):
            offsets = np.roll(direction, i)
            offsets_list.append(offsets.copy())
            ranks_send.append(rank_neighbouring_domain(*(+offsets)))
            ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
            if only_supply:
                offsets_list.append(-offsets)
                ranks_send.append(rank_neighbouring_domain(*(-offsets)))
                ranks_recv.append(rank_neighbouring_domain(*(+offsets)))
        # - Domains at the 12 edges
        #   (when only_supply is False, send
        #     {right  , forward}, {left     , forward },
        #     {forward, upward }, {backward , upward  },
        #     {right  , upward }, {rightward, downward},
        # )
        direction = np.array([+1, +1,  0], dtype=C2np['int'])
        flip      = np.array([-1, +1, +1], dtype=C2np['int'])
        for i in range(3):
            offsets = np.roll(direction, i)
            offsets_list.append(offsets.copy())
            ranks_send.append(rank_neighbouring_domain(*(+offsets)))
            ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
            if only_supply:
                offsets_list.append(-offsets)
                ranks_send.append(rank_neighbouring_domain(*(-offsets)))
                ranks_recv.append(rank_neighbouring_domain(*(+offsets)))
            offsets *= np.roll(flip, i)
            offsets_list.append(offsets.copy())
            ranks_send.append(rank_neighbouring_domain(*(+offsets)))
            ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
            if only_supply:
                offsets_list.append(-offsets)
                ranks_send.append(rank_neighbouring_domain(*(-offsets)))
                ranks_recv.append(rank_neighbouring_domain(*(+offsets)))
        # - Domains at the 8 corners
        #   (when only_supply is False, send
        #    {right, forward , upward  },
        #    {right, forward , downward},
        #    {left , forward , upward  },
        #    {right, backward, upward  },
        # )
        offsets = np.array([+1, +1, +1], dtype=C2np['int'])
        offsets_list.append(offsets.copy())
        ranks_send.append(rank_neighbouring_domain(*(+offsets)))
        ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
        if only_supply:
            offsets_list.append(-offsets)
            ranks_send.append(rank_neighbouring_domain(*(-offsets)))
            ranks_recv.append(rank_neighbouring_domain(*(+offsets)))
        direction = np.array([+1, +1, -1], dtype=C2np['int'])
        for i in range(3):
            offsets = np.roll(direction, i)
            offsets_list.append(offsets.copy())
            ranks_send.append(rank_neighbouring_domain(*(+offsets)))
            ranks_recv.append(rank_neighbouring_domain(*(-offsets)))
            if only_supply:
                offsets_list.append(-offsets)
                ranks_send.append(rank_neighbouring_domain(*(-offsets)))
                ranks_recv.append(rank_neighbouring_domain(*(+offsets)))
        domain_domain_communication_dict[pairing_level, only_supply] = (
            (np.array(ranks_send, dtype=C2np['int']), np.array(ranks_recv, dtype=C2np['int']))
        )
        domain_domain_communication_dict[pairing_level, only_supply, 'domain_pair_offsets'] = (
            np.array(offsets_list, dtype=C2np['Py_ssize_t'])
        )
    else:
        abort(
            f'domain_domain_communication() got '
            f'pairing_level = {pairing_level} ∉ {{"domain", "tile"}}'
        )
    return domain_domain_communication_dict[pairing_level, only_supply]
# Cached results of the domain_domain_communication function
# are stored in the dict below.
cython.declare(domain_domain_communication_dict=dict)
domain_domain_communication_dict = {}

# Function that given arrays of receiver and supplier tiles
# returns them in paired format.
@cython.header(
    # Arguments
    receiver='Component',
    supplier='Component',
    tile_indices_receiver='Py_ssize_t[::1]',
    tile_indices_supplier='Py_ssize_t[::1]',
    rank_supplier='int',
    only_supply_passed='bint',
    domain_pair_nr='Py_ssize_t',
    interaction_name=str,
    # Locals
    dim='int',
    domain_pair_offset='Py_ssize_t[::1]',
    global_tile_layout_shape='Py_ssize_t[::1]',
    i='Py_ssize_t',
    j='Py_ssize_t',
    key=tuple,
    l='Py_ssize_t',
    l_offset='Py_ssize_t',
    l_s='Py_ssize_t',
    m='Py_ssize_t',
    m_offset='Py_ssize_t',
    m_s='Py_ssize_t',
    n='Py_ssize_t',
    n_offset='Py_ssize_t',
    n_s='Py_ssize_t',
    neighbourtile_index_3D_global='Py_ssize_t[::1]',
    pairings='Py_ssize_t**',
    pairings_N='Py_ssize_t*',
    pairs_N='Py_ssize_t',
    suppliertile_indices_3D_global_to_1D_local=dict,
    tile_index_3D_global_s=tuple,
    tile_index_r='Py_ssize_t',
    tile_index_s='Py_ssize_t',
    tile_indices_supplier_paired='Py_ssize_t[::1]',
    tile_indices_supplier_paired_ptr='Py_ssize_t*',
    tile_layout='Py_ssize_t[:, :, ::1]',
    tile_pairings_index='Py_ssize_t',
    tiling='Tiling',
    tiling_name=str,
    wraparound='bint',
    returns='Py_ssize_t',
)
def get_tile_pairings(
    receiver, supplier, tile_indices_receiver, tile_indices_supplier,
    rank_supplier, only_supply_passed, domain_pair_nr, interaction_name,
):
    global tile_pairings_cache, tile_pairings_N_cache, tile_pairings_cache_size
    # Lookup index of the required tile pairings in the global cache
    key = (receiver.name, supplier.name, interaction_name, domain_pair_nr)
    tile_pairings_index = tile_pairings_cache_indices.get(key, tile_pairings_cache_size)
    if tile_pairings_index < tile_pairings_cache_size:
        return tile_pairings_index
    # No cached results found. We will now compute the supplier tile
    # indices to be paired with each of the receiver tiles.
    # Below is a list of lists storing the supplier tile indices
    # for each receiver tile. The type of this data structure will
    # change during the computation.
    tile_indices_receiver_supplier = [[] for i in range(tile_indices_receiver.shape[0])]
    # Get the shape of the local (domain) tile layout,
    # as well as of the global (box) tile layout.
    tiling_name = f'{interaction_name} (tiles)'
    tiling = receiver.tilings[tiling_name]
    tile_layout = tiling.layout
    tile_layout_shape = asarray(asarray(tile_layout).shape)
    # The general computation below takes a long time when dealing with
    # many tiles. By far the worst case is when all tiles in the local
    # domain should be paired with themselves, which is the case for
    # domain_pair_nr == 0. For this case we perform a much faster,
    # more specialised computation.
    if domain_pair_nr == 0:
        if rank != rank_supplier:
            abort(
                f'get_tile_pairings() got rank_supplier = {rank_supplier} != rank = {rank} '
                f'at domain_pair_nr == 0'
            )
        if not np.all(asarray(tile_indices_receiver) == asarray(tile_indices_supplier)):
            abort(
                f'get_tile_pairings() got tile_indices_receiver != tile_indices_supplier '
                f'at domain_pair_nr == 0'
            )
        i = 0
        for         l in range(tile_layout.shape[0]):
            for     m in range(tile_layout.shape[1]):
                for n in range(tile_layout.shape[2]):
                    if i != tile_layout[l, m, n]:
                        abort(
                            f'It looks as though the tile layout of {receiver.name} is incorrect'
                        )
                    neighbourtile_indices_supplier = tile_indices_receiver_supplier[i]
                    for l_offset in range(-1, 2):
                        l_s = l + l_offset
                        if l_s == -1 or l_s == ℤ[tile_layout.shape[0]]:
                            continue
                        for m_offset in range(-1, 2):
                            m_s = m + m_offset
                            if m_s == -1 or m_s == ℤ[tile_layout.shape[1]]:
                                continue
                            for n_offset in range(-1, 2):
                                n_s = n + n_offset
                                if n_s == -1 or n_s == ℤ[tile_layout.shape[2]]:
                                    continue
                                tile_index_s = tile_layout[l_s, m_s, n_s]
                                if tile_index_s >= i:
                                    neighbourtile_indices_supplier.append(tile_index_s)
                    tile_indices_receiver_supplier[i] = asarray(
                        neighbourtile_indices_supplier, dtype=C2np['Py_ssize_t'],
                    )
                    i += 1
    else:
        # Get relative offsets of the domains currently being paired
        domain_pair_offset = domain_domain_communication_dict[
            'tile', only_supply_passed, 'domain_pair_offsets'][domain_pair_nr, :]
        # Get the indices of the global domain layout matching the
        # receiver (local) domain and supplier domain.
        domain_layout_receiver_indices = asarray(
            np.unravel_index(rank, domain_subdivisions)
        )
        domain_layout_supplier_indices = asarray(
            np.unravel_index(rank_supplier, domain_subdivisions)
        )
        global_tile_layout_shape = asarray(
            asarray(domain_subdivisions)*tile_layout_shape,
            dtype=C2np['Py_ssize_t'],
        )
        tile_index_3D_r_start = domain_layout_receiver_indices*tile_layout_shape
        tile_index_3D_s_start = domain_layout_supplier_indices*tile_layout_shape
        # Construct dict mapping global supplier 3D indices to their
        # local 1D counterparts.
        suppliertile_indices_3D_global_to_1D_local = {}
        for j in range(tile_indices_supplier.shape[0]):
            tile_index_s = tile_indices_supplier[j]
            tile_index_3D_s = asarray(tiling.tile_index3D(tile_index_s))
            tile_index_3D_global_s = tuple(tile_index_3D_s + tile_index_3D_s_start)
            suppliertile_indices_3D_global_to_1D_local[tile_index_3D_global_s] = tile_index_s
        # Pair each receiver tile with all neighbouring supplier tiles
        for i in range(tile_indices_receiver.shape[0]):
            neighbourtile_indices_supplier = tile_indices_receiver_supplier[i]
            # Construct global 3D index of this receiver tile
            tile_index_r = tile_indices_receiver[i]
            tile_index_3D_r = asarray(tiling.tile_index3D(tile_index_r))
            tile_index_3D_global_r = tile_index_3D_r + tile_index_3D_r_start
            # Loop over all neighbouring receiver tiles
            # (including the tile itself).
            for         l in range(-1, 2):
                for     m in range(-1, 2):
                    for n in range(-1, 2):
                        neighbourtile_index_3D_global = asarray(
                            tile_index_3D_global_r + asarray((l, m, n)),
                            dtype=C2np['Py_ssize_t'],
                        )
                        # For domain_pair_nr == 0, all tiles in the
                        # local domain are paired with all others.
                        # Here we must not take the periodicity into
                        # account, as such interacions are performed by
                        # future domain pairings.
                        with unswitch:
                            if domain_pair_nr == 0:
                                wraparound = False
                                for dim in range(3):
                                    if not (
                                        0 <= neighbourtile_index_3D_global[dim]
                                          <  global_tile_layout_shape[dim]
                                    ):
                                        wraparound = True
                                        break
                                if wraparound:
                                    continue
                        # Take the periodicity of the domain layout
                        # into account. This should only be done
                        # along the direction(s) connecting
                        # the paired domains.
                        with unswitch:
                            if 𝔹[domain_pair_offset[0] != 0]:
                                neighbourtile_index_3D_global[0] = mod(
                                    neighbourtile_index_3D_global[0],
                                    global_tile_layout_shape[0],
                                )
                        with unswitch:
                            if 𝔹[domain_pair_offset[1] != 0]:
                                neighbourtile_index_3D_global[1] = mod(
                                    neighbourtile_index_3D_global[1],
                                    global_tile_layout_shape[1],
                                )
                        with unswitch:
                            if 𝔹[domain_pair_offset[2] != 0]:
                                neighbourtile_index_3D_global[2] = mod(
                                    neighbourtile_index_3D_global[2],
                                    global_tile_layout_shape[2],
                                )
                        # Check if a supplier tile sits at the location
                        # of the current neighbour tile.
                        tile_index_s = suppliertile_indices_3D_global_to_1D_local.get(
                            tuple(neighbourtile_index_3D_global),
                            -1,
                        )
                        if tile_index_s != -1:
                            # For domain_pair_nr == 0, all tiles in the
                            # local domain are paired with all others.
                            # To not double count, we disregard the
                            # pairing if the supplier index is lower
                            # than the receiver.
                            with unswitch:
                                if domain_pair_nr == 0:
                                    if tile_index_s < tile_index_r:
                                        continue
                            neighbourtile_indices_supplier.append(tile_index_s)
            # Convert the neighbouring supplier tile indices from a list
            # to an Py_ssize_t[::1] array.
            # We also sort the indices, though this is not necessary.
            neighbourtile_indices_supplier = asarray(
                neighbourtile_indices_supplier, dtype=C2np['Py_ssize_t'],
            )
            neighbourtile_indices_supplier.sort()
            tile_indices_receiver_supplier[i] = neighbourtile_indices_supplier
    # Transform tile_indices_receiver_supplier to an object array,
    # the elements of which are arrays of dtype Py_ssize_t.
    tile_indices_receiver_supplier = asarray(tile_indices_receiver_supplier, dtype=object)
    # If all arrays in tile_indices_receiver_supplier are of the
    # same size, it will not be stored as an object array of Py_ssize_t
    # arrays, but instead a 2D object array. In compiled mode this leads
    # to a crash, as elements of tile_indices_receiver_supplier must be
    # compatible with Py_ssize_t[::1]. We can convert it to a 2D
    # Py_ssize_t array instead, single-index elements of which exactly
    # are the Py_ssize_t[::1] arrays we need. When the arrays in
    # tile_indices_receiver_supplier are not of the same size, such a
    # conversion will fail. Since we do not care about the conversion
    # in such a case anyway, we always just attempt to do
    # the conversion. If it succeed, it was needed. If not, it was not
    # needed anyway.
    try:
        tile_indices_receiver_supplier = asarray(
            tile_indices_receiver_supplier, dtype=C2np['Py_ssize_t'])
    except ValueError:
        pass
    # Cache the result. This cache is not actually used, but it ensures
    # that Python will not garbage collect the data.
    tile_indices_receiver_supplier_dict[key] = tile_indices_receiver_supplier
    # Now comes the caching that is actually used, where we use pointers
    # rather than Python objects.
    pairs_N = tile_indices_receiver_supplier.shape[0]
    pairings   = malloc(pairs_N*sizeof('Py_ssize_t*'))
    pairings_N = malloc(pairs_N*sizeof('Py_ssize_t'))
    for i in range(pairs_N):
        tile_indices_supplier_paired = tile_indices_receiver_supplier[i]
        tile_indices_supplier_paired_ptr = cython.address(tile_indices_supplier_paired[:])
        pairings[i] = tile_indices_supplier_paired_ptr
        pairings_N[i] = tile_indices_supplier_paired.shape[0]
    tile_pairings_cache_size += 1
    tile_pairings_cache = realloc(
        tile_pairings_cache, tile_pairings_cache_size*sizeof('Py_ssize_t**'),
    )
    tile_pairings_N_cache = realloc(
        tile_pairings_N_cache, tile_pairings_cache_size*sizeof('Py_ssize_t*'),
    )
    tile_pairings_cache  [tile_pairings_index] = pairings
    tile_pairings_N_cache[tile_pairings_index] = pairings_N
    tile_pairings_cache_indices[key] = tile_pairings_index
    return tile_pairings_index
# Caches used by the get_tile_pairings function
cython.declare(
    tile_indices_receiver_supplier_dict=dict,
    tile_pairings_cache_size='Py_ssize_t',
    tile_pairings_cache_indices=dict,
    tile_pairings_cache='Py_ssize_t***',
    tile_pairings_N_cache='Py_ssize_t**',
)
tile_indices_receiver_supplier_dict = {}
tile_pairings_cache_size = 0
tile_pairings_cache_indices = {}
tile_pairings_cache   = malloc(tile_pairings_cache_size*sizeof('Py_ssize_t**'))
tile_pairings_N_cache = malloc(tile_pairings_cache_size*sizeof('Py_ssize_t*'))

# Generic function implementing particle-mesh interactions
# for both particle and fluid componenets.
@cython.header(
    # Arguments
    receivers=list,
    suppliers=list,
    quantity=str,
    φ_gridsizes_receivers=list,
    potential=func_potential,
    potential_name=str,
    interpolation_order='int',
    interlace='bint',
    differentiation_order='int',
    ᔑdt=dict,
    ᔑdt_key=object,  # str or tuple
    # Locals
    J_dim='FluidScalar',
    J_dim_ptr='double*',
    component='Component',
    components=list,
    dim='int',
    grid='double[:, :, ::1]',
    grid_interpolated='double[:, :, ::1]',
    grids=dict,
    i='Py_ssize_t',
    receiver_group=dict,
    receiver_groups=dict,
    representation=str,
    Δx_φ='double',
    φ_gridsize='Py_ssize_t',
    ϱ_ptr='double*',
    𝒫_ptr='double*',
    ᐁgrid_dim='double[:, :, ::1]',
    ᐁgrid_dim_ptr='double*',
)
def particle_mesh(
    receivers, suppliers, quantity, φ_gridsizes_receivers, potential, potential_name,
    interpolation_order, interlace, differentiation_order, ᔑdt, ᔑdt_key,
):
    """This function will update the momenta of all receiver components
    due to an interaction. This is done by constructing global fields by
    interpolating the dependent variables of all suppliers onto grids.
    Two global grids are used, one for particles and one for fluids,
    both of which contain the entire potential field of both particles
    and fluid components. These grids are constructed in the
    construct_potential() function.

    This function is then responsible for differentiating the potential
    grids and applying the resulting force to the receiver components.
    This force is applied given the prescription
    Δmom = -mass*∂ⁱφ*ᔑdt[ᔑdt_key].
    """
    # Build the two potentials due to all particles and fluid suppliers.
    # For the potential gridsize, we always choose the largest of the
    # available φ_gridsizes of the receivers.
    masterprint(
        f'Constructing the {potential_name} due to {suppliers[0].name} ...'
        if len(suppliers) == 1 else (
            f'Constructing the {potential_name} due to {{{{{{}}}}}} ...'
            .format(', '.join([component.name for component in suppliers]))
        )
    )
    φ_gridsize = np.max(φ_gridsizes_receivers)
    grids = construct_potential(
        receivers, suppliers, quantity, φ_gridsize, potential, interpolation_order, interlace, ᔑdt,
    )
    masterprint('done')
    # Group receivers into a dict mapping representation to
    # dict mapping φ_gridsize to list of receiver components.
    receiver_groups = {
        representation: collections.defaultdict(list)
        for representation in ('particles', 'fluid')
    }
    for φ_gridsize, component in zip(φ_gridsizes_receivers, receivers):
        receiver_groups[component.representation][φ_gridsize].append(component)
    for representation in ('particles', 'fluid'):
        receiver_groups[representation] = {
            φ_gridsize: list(receiver_groups[representation][φ_gridsize])
            for φ_gridsize in sorted(receiver_groups[representation].keys(), reverse=True)
        }
    # Buffers to use for interpolation and differentiation.
    buffer_name_interpolate   = 0
    buffer_name_differentiate = 1
    # Loop over the receiver components and apply the force to each
    for representation, receiver_group in receiver_groups.items():
        grid = grids[representation]
        for φ_gridsize, components in receiver_group.items():
            # Interpolate potential grid to new grid
            # of gridsize φ_gridsize.
            grid_interpolated = interpolate_grid_to_grid(grid, buffer_name_interpolate, φ_gridsize)
            # For each dimension, differentiate the potential
            # and apply the force to the selected components.
            Δx_φ = boxsize/φ_gridsize  # Physical grid spacing of potential grid
            for dim in range(3):
                masterprint(
                    f'Differentiating the ({representation}) {potential_name} along the '
                    f'{"xyz"[dim]}-direction and applying it ...'
                )
                # Differentiate the grid along the dim'th dimension
                ᐁgrid_dim = diff_domaingrid(
                    grid_interpolated, dim, differentiation_order, Δx_φ, buffer_name_differentiate,
                )
                # Apply force
                for component in components:
                    with unswitch:
                        if isinstance(ᔑdt_key, tuple):
                            ᔑdt_key = (ᔑdt_key[0], component.name)
                    masterprint(f'Applying to {component.name} ...')
                    with unswitch(3):
                        if representation == 'particles':
                            # Update the dim'th momentum component of
                            # all particles through interpolation in
                            # ᐁgrid_dim. To convert from force to
                            # momentum change we should multiply by
                            # -mass*Δt (minus as the force is the
                            # negative gradient of the potential), where
                            # Δt = ᔑdt['1']. Here this integral over the
                            # time step is generalised and supplied by
                            # the caller.
                            interpolate_domaingrid_to_particles(
                                ᐁgrid_dim, component, 'mom', dim, interpolation_order,
                                component.mass*ℝ[-ᔑdt[ᔑdt_key]],
                            )
                        else:  # representation == 'fluid'
                            # The source term has the form
                            # ΔJ ∝ -(ϱ + c⁻²𝒫)*ᐁφ.
                            # The proportionality factor above is
                            # something liḱe Δt = ᔑdt['1']. Here this
                            # integral over the time step is generalised
                            # and supplied by the caller. As we are
                            # guaranteed that φ_gridsize matches the
                            # fluid gridsize, we simply add the values
                            # directly; no additional interpolation is
                            # needed.
                            J_dim = component.J[dim]
                            J_dim_ptr = J_dim.grid
                            ϱ_ptr = component.ϱ.grid
                            𝒫_ptr = component.𝒫.grid
                            ᐁgrid_dim_ptr = cython.address(ᐁgrid_dim[:, :, :])
                            for i in range(component.size):
                                J_dim_ptr[i] += ℝ[-ᔑdt[ᔑdt_key]]*(
                                    ϱ_ptr[i] + ℝ[light_speed**(-2)]*𝒫_ptr[i]
                                )*ᐁgrid_dim_ptr[i]
                            # If the ghost points of J_dim was properly
                            # populated prior to the momentum update,
                            # they should have been correctly updated as
                            # well. To be absolutely sure, we here set
                            # the ghost points from the boundary points.
                            communicate_ghosts(J_dim.grid_mv, '=')
                    masterprint('done')
                masterprint('done')

# Generic function capable of constructing potential grids out of
# components and a given expression for the potential.
@cython.header(
    # Arguments
    receivers=list,
    suppliers=list,
    quantity=str,
    φ_gridsize='Py_ssize_t',
    potential=func_potential,
    order='int',
    interlace='bint',
    ᔑdt=dict,
    # Locals
    deconv='double',
    deconv_ij='double',
    deconv_ijk='double',
    deconv_j='double',
    fft_normalization_factor='double',
    grid='double[:, :, ::1]',
    grids=dict,
    gridshape_local=tuple,
    i='Py_ssize_t',
    im='double',
    j='Py_ssize_t',
    j_global='Py_ssize_t',
    k='Py_ssize_t',
    k2='Py_ssize_t',
    ki='Py_ssize_t',
    ki_plus_kj='Py_ssize_t',
    kj='Py_ssize_t',
    kk='Py_ssize_t',
    potential_factor='double',
    present=dict,
    re='double',
    representation=str,
    representation_counter='int',
    slab='double[:, :, ::1]',
    slab_fluid='double[:, :, ::1]',
    slab_fluid_jik='double*',
    slab_jik='double*',
    slab_particles_jik='double*',
    slab_particles_shifted='double[:, :, ::1]',
    slab_particles_shifted_jik='double*',
    slabs=dict,
    θ='double',
    returns=dict,
)
def construct_potential(
    receivers, suppliers, quantity, φ_gridsize, potential, order, interlace, ᔑdt,
):
    """This function populates two grids (including ghost layers) with a
    real-space potential corresponding to the Fourier-space potential
    function given, due to all supplier components. A seperate grid for
    particle and fluid components will be constructed, the difference
    being only the handling of deconvolutions needed for the
    interpolation to/from the grid. Both grids will contain the full
    potential due to all the supplier components. Which variables to
    extrapolate to the grid(s) is determined by the quantities argument.
    For details on this argument, see the interpolate_components()
    function in the mesh module.

    First the variable given in 'quantity' of the supplier components
    are interpolated to the grids; particle components to one grid and
    fluid components to a seperate grid. The two grids are then Fourier
    transformed.
    The potential function is then used to change the value of each grid
    point for both grids. Also while in Fourier space, deconvolutions
    will be carried out, in a different manner for each grid.
    The two grids are added in such a way that they both corresponds to
    the total potential of all components, but deconvolved in the way
    suitable for either particles or fluids. The two grids are now
    Fourier transformed back to real space.

    The order argument specifies the interpolation order; 1 for NGP,
    2 for CIC, 3 for TSC, 4 for PCS.

    In the case of normal gravity, we have
    φ(k) = -4πGa²ρ(k)/k² = -4πG a**(-3*w_eff - 1) ϱ(k)/k²,
    which can be signalled by passing
    quantities = [('particles', a**(-3*w_eff - 1)*mass/Vcell),
                  ('ϱ', a**(-3*w_eff - 1))],
    potential = lambda k2: -4*π*G_Newton/k2
    (note that it is not actally allowed to pass an untyped lambda
    function in compiled mode).
    """
    # Dicts of flags specifying whether any fluid/particle components
    # are present among the receivers/suppliers.
    present = {
        (representation, components_type): (
            representation in {component.representation for component in components}
        )
        for representation in ('fluid', 'particles')
        for components_type, components in zip(('receivers', 'suppliers'), (receivers, suppliers))
    }
    if not present['particles', 'receivers'] and not present['fluid', 'receivers']:
        abort('construct_potential() got no recognizable receivers')
    if not present['particles', 'suppliers'] and not present['fluid', 'suppliers']:
        abort('construct_potential() got no recognizable suppliers')
    # Interpolate the particles/fluid elements onto grids
    grids = interpolate_components(suppliers, quantity, φ_gridsize, order, ᔑdt, interlace)
    # If a given representation does not exist among the suppliers, the
    # corresponding grids[representation] will be None. If at the same
    # time we do have this representation among the receivers, we really
    # do need a (nullified) grid.
    for grid in grids.values():
        if grid is not None:
            gridshape_local = asarray(grid).shape
            break
    for representation in ('fluid', 'particles'):
        grid = grids[representation]
        if grid is None and present[representation, 'receivers']:
            grids[representation] = get_buffer(gridshape_local, f'grid_{representation}',
                nullify=True)
    # Slab decompose the grids
    slabs = {
        representation: slab_decompose(grid, f'slab_{representation}', prepare_fft=True)
        for representation, grid in grids.items()
    }
    # Do a forward in-place Fourier transform of the slabs
    for slab in slabs.values():
        fft(slab, 'forward')
    # Store the fluid slab as a separate variable. Also, if we had any
    # particle supplier and interlace is True, a slab named
    # 'particles_shifted' will be present. Store this as a separate
    # variable as well.
    slab_fluid = slabs['fluid']
    slab_particles_shifted = slabs.get('particles_shifted')
    # In the case of both particle and fluid components being present,
    # it is important that the particle slabs are handled after the
    # fluid slabs, as the deconvolution factor is only computed for
    # particle components and this is needed after combining the fluid
    # and particle slabs. It is also important that the order of
    # representations in grids and slabs is the same.
    slabs = {
        representation: slabs[representation] for representation in ('fluid', 'particles')
    }
    grids = {
        representation: grids[representation] for representation in ('fluid', 'particles')
    }
    # Multiplicative factor needed after a forward and a backward
    # Fourier transformation.
    fft_normalization_factor = float(φ_gridsize)**(-3)
    # For each grid, multiply by the potential and deconvolution
    # factors. Do fluid slabs fist, then particle slabs.
    for representation_counter, (representation, slab) in enumerate(slabs.items()):
        # No need to process the fluid slab if it does not
        # contain any data.
        if representation == 'fluid' and not 𝔹[present['fluid', 'suppliers']]:
            continue
        # Do not apply any deconvolution for the fluid slab.
        # For the particle slab, this will be redefined below.
        deconv = 1
        # Begin loop over slabs. As the first and second dimensions
        # are transposed due to the FFT, start with the j-dimension.
        for j in range(ℤ[slab.shape[0]]):
            # The j-component of the wave vector (grid units).
            # Since the slabs are distributed along the j-dimension,
            # an offset must be used.
            j_global = ℤ[slab.shape[0]*rank] + j
            kj = j_global - φ_gridsize if j_global > ℤ[φ_gridsize//2] else j_global
            # The j-component of the deconvolution
            with unswitch(1):
                if 𝔹[representation == 'particles']:
                    deconv_j = get_deconvolution(kj*ℝ[π/φ_gridsize])
            # Loop through the complete i-dimension
            for i in range(φ_gridsize):
                # The i-component of the wave vector (grid units)
                ki = i - φ_gridsize if i > ℤ[φ_gridsize//2] else i
                # The product of the i- and the j-component
                # of the deconvolution.
                with unswitch(2):
                    if 𝔹[representation == 'particles']:
                        deconv_ij = get_deconvolution(ki*ℝ[π/φ_gridsize])*deconv_j
                # The sum of wave vector elements
                with unswitch(2):
                    if 𝔹[representation == 'particles' and slab_particles_shifted is not None]:
                        ki_plus_kj = ki + kj
                # Loop through the complete, padded k-dimension
                # in steps of 2 (one complex number at a time).
                for k in range(0, ℤ[slab.shape[2]], 2):
                    # The k-component of the wave vector (grid units)
                    kk = k//2
                    # The squared magnitude of the wave vector (grid units)
                    k2 = ℤ[ℤ[kj**2] + ki**2] + kk**2
                    # Pointer to the [j, i, k]'th element of the slab.
                    # The complex number is then given as
                    # Re = slab_jik[0], Im = slab_jik[1].
                    slab_jik = cython.address(slab[j, i, k:])
                    # Enforce the vanishing of the potential at |k| = 0.
                    # The real-space mean value of the potential will
                    # then be zero, as it should for a
                    # peculiar potential.
                    if k2 == 0:
                        slab_jik[0] = 0  # Real part
                        slab_jik[1] = 0  # Imag part
                        continue
                    # The final deconvolution factor
                    with unswitch(3):
                        if 𝔹[representation == 'particles']:
                            # The total (NGP) deconvolution factor
                            deconv_ijk = deconv_ij*get_deconvolution(kk*ℝ[π/φ_gridsize])
                            # The full deconvolution factor
                            deconv_ijk **= order
                            # A deconvolution of the particle potential
                            # is needed due to the interpolation from
                            # the particle positions to the grid.
                            deconv = deconv_ijk
                            # For particle receivers we will need to do
                            # a second deconvolution due to the
                            # interpolation from the grid back to the
                            # particles. In the case where we have only
                            # particle components and thus only a
                            # particles potential, we carry out this
                            # second deconvolution now. If both particle
                            # and fluid components are present,
                            # this second deconvolution
                            # will take place later.
                            with unswitch(4):
                                if 𝔹[
                                        not present['fluid', 'receivers']
                                    and not present['fluid', 'suppliers']
                                ]:
                                    deconv *= deconv_ijk
                    # Interlace the two relatively shifted particle
                    # slabs using harmonic averaging. The result
                    # overwrites the current values
                    # in the particles slab.
                    with unswitch(3):
                        if 𝔹[representation == 'particles' and slab_particles_shifted is not None]:
                            slab_particles_shifted_jik = cython.address(
                                slab_particles_shifted[j, i, k:])
                            re, im = slab_particles_shifted_jik[0], slab_particles_shifted_jik[1]
                            θ = ℝ[π/φ_gridsize]*(ki_plus_kj + kk)
                            re, im = re*ℝ[cos(θ)] - im*ℝ[sin(θ)], re*ℝ[sin(θ)] + im*ℝ[cos(θ)]
                            slab_particles_jik = slab_jik
                            slab_particles_jik[0] = 0.5*(slab_particles_jik[0] + re)  # Real part
                            slab_particles_jik[1] = 0.5*(slab_particles_jik[1] + im)  # Imag part
                    # Transform this complex grid point.
                    # The particles grid only need to be processed if it
                    # contains data (i.e. particle suppliers exist).
                    with unswitch(3):
                        if 𝔹[representation == 'fluid' or present['particles', 'suppliers']]:
                            # The physical squared length of the wave
                            # vector is given by (2π/boxsize*|k|)².
                            potential_factor = potential(ℝ[(2*π/boxsize)**2]*k2)
                            slab_jik[0] *= ℝ[  # Real part
                                potential_factor*deconv*fft_normalization_factor
                            ]
                            slab_jik[1] *= ℝ[  # Imag part
                                potential_factor*deconv*fft_normalization_factor
                            ]
                    # If only particle components or only fluid
                    # components exist, the slabs now store the final
                    # potential in Fourier space. However, if both
                    # particle and fluid components exist, the two sets
                    # of slabs should be combined to form total
                    # potentials.
                    with unswitch(3):
                        if 𝔹[
                                representation_counter == 1
                            and (
                                   present['particles', 'receivers']
                                or present['particles', 'suppliers']
                            )
                            and (
                                   present['fluid', 'receivers']
                                or present['fluid', 'suppliers']
                            )
                        ]:
                            # Pointers to this element for both slabs.
                            # As we are looping over the particle slab,
                            # we may reuse the pointer above.
                            slab_particles_jik = slab_jik
                            slab_fluid_jik = cython.address(slab_fluid[j, i, k:])
                            # Add the particle potential values
                            # to the fluid potential.
                            slab_fluid_jik[0] += slab_particles_jik[0]  # Real part
                            slab_fluid_jik[1] += slab_particles_jik[1]  # Imag part
                            # Now the fluid slabs store the total
                            # potential, with the particle part
                            # deconvolved once due to the interpolation
                            # of the particles to the grid. The particle
                            # slabs should now be a copy of what is
                            # stored in the fluid slabs, but with an
                            # additional deconvolution, accounting for
                            # the upcoming interpolation from the grid
                            # back to the particles.
                            slab_particles_jik[0] = deconv_ijk*slab_fluid_jik[0]  # Real part
                            slab_particles_jik[1] = deconv_ijk*slab_fluid_jik[1]  # Imag part
    # If a representation is present amongst the suppliers but not the
    # receivers, the corresponding (total) potential has been
    # constructed but will not be used. Remove it.
    for representation in grids:
        if present[representation, 'suppliers'] and not present[representation, 'receivers']:
            grids[representation] = None
            slabs[representation] = None
    if slabs['particles'] is None and slabs['fluid'] is None:
        abort(
            'Something went wrong in the construct_potential() function, '
            'as it appears that neither particles nor fluids should receive the force '
            'due to the potential'
        )
    # Fourier transform the slabs back to coordinate space
    for slab in slabs.values():
        fft(slab, 'backward')
    # Domain-decompose the slabs
    for grid, slab in zip(grids.values(), slabs.values()):
        domain_decompose(slab, grid)  # Also populates ghosts
    # Return the potential grid(s)
    return grids

# Function that carries out the gravitational interaction
@cython.pheader(
    # Arguments
    method=str,
    receivers=list,
    suppliers=list,
    ᔑdt=dict,
    interaction_type=str,
    printout='bint',
    pm_potential=str,
    φ_gridsizes_receivers=list,
    interpolation_order='int',
    interlace=object,  # bool or NoneType
    differentiation_order='int',
    # Locals
    potential=func_potential,
    potential_name=str,
    quantity=str,
    φ_gridsize_max_suppliers='Py_ssize_t',
    ᔑdt_key=object,  # str or tuple
)
def gravity(
    method, receivers, suppliers, ᔑdt, interaction_type, printout,
    pm_potential='full', φ_gridsizes_receivers=None,
    interpolation_order=-1, interlace=None, differentiation_order=-1,
):
    # Compute gravity via one of the following methods
    if method == 'p3m':
        # The particle-particle-mesh method
        if printout:
            if 𝔹['long' in interaction_type]:
                extra_message = ' (long-range only)'
            elif 𝔹['short' in interaction_type]:
                extra_message = ' (short-range only)'
            else:
                extra_message = ''
            masterprint(
                'Executing',
                shortrange_progress_messages('gravity', method, receivers, extra_message),
                '...',
            )
        # The long-range PM part
        if 𝔹['any' in interaction_type] or 𝔹['long' in interaction_type]:
            if not φ_gridsizes_receivers:
                φ_gridsizes_receivers = [
                    component.φ_gridsizes['gravity', 'p3m'] for component in receivers
                ]
            if interpolation_order == -1:
                interpolation_order = ℤ[force_interpolations['gravity']['p3m']]
            if interlace is None:
                interlace = ℤ[force_interlacings['gravity']['p3m']]
            if differentiation_order == -1:
                differentiation_order = ℤ[force_differentiations['gravity']['p3m']]
            gravity(
                'pm', receivers, suppliers, ᔑdt, interaction_type, printout,
                'long-range only', φ_gridsizes_receivers,
                interpolation_order, interlace, differentiation_order,
            )
        # The short-range PP part
        if 𝔹['any' in interaction_type] or 𝔹['short' in interaction_type]:
            tabulate_shortrange_gravity()
            component_component(
                receivers, suppliers, gravity_pairwise_shortrange, ᔑdt,
                dependent=['pos'],
                affected=['mom'],
                deterministic=True,
                pairing_level='tile',
                interaction_name='gravity',
            )
        if printout:
            masterprint('done')
    elif method == 'pm':
        # The particle-mesh method.
        if pm_potential == 'full':
            # Use the full gravitational potential
            if printout:
                masterprint(
                    f'Executing gravitational interaction for {receivers[0].name} '
                    f'via the PM method ...'
                    if len(receivers) == 1 else (
                        'Executing gravitational interaction for {{{}}} via the PM method ...'
                        .format(', '.join([component.name for component in receivers]))
                    )
                )
            potential = gravity_potential
            potential_name = 'gravitational potential'
        elif 'long' in pm_potential:
            # Only use the long-range part of the
            # gravitational potential.
            potential = gravity_longrange_potential
            potential_name = 'gravitational long-range potential'
        elif master:
            abort(f'Unrecognized pm_potential = {pm_potential} in gravity()')
        # The gravitational potential is given by the Poisson equation
        # ∇²φ = 4πGa²ρ = 4πGa**(-3*w_eff - 1)ϱ,
        # summed over all suppliers. The component dependent quantity
        # is then
        # a²ρ = a**(-3*w_eff - 1)ϱ.
        quantity = 'a²ρ'
        # In the fluid description, the gravitational source term is
        # ∂ₜJⁱ = ⋯ -a**(-3*w_eff)*(ϱ + c⁻²𝒫)*∂ⁱφ
        # and so a**(-3*w_eff) should be integrated over the time step
        # to get ΔJⁱ. In the particle description, the gravitational
        # source term is
        # ∂ₜmomⁱ = -mass*∂ⁱφ.
        # In the general case of a changing mass, the current mass is
        # given by mass*a**(-3*w_eff), and so again, a**(-3*w_eff)
        # shoud be integrated over the time step
        # in order to obtain Δmomⁱ.
        ᔑdt_key = ('a**(-3*w_eff)', 'component')
        # Execute the gravitational particle-mesh interaction
        if not φ_gridsizes_receivers:
            # It may happen that a receiver does not have an assgined
            # φ_gridsize for gravity PM because it really wants to
            # receiver gravity via another method (e.g. P³M), but this
            # has been switched out with PM for interactions with fluid
            # suppliers. Set φ_gridsize of such a receiver to the
            # maximum φ_gridsize of the suppliers.
            φ_gridsize_max_suppliers = np.max([
                component.φ_gridsizes.get(('gravity', 'pm'), -1) for component in suppliers
            ])
            φ_gridsizes_receivers = [
                component.φ_gridsizes.get(('gravity', 'pm'), φ_gridsize_max_suppliers)
                for component in receivers
            ]
        if interpolation_order == -1:
            interpolation_order = ℤ[force_interpolations['gravity']['pm']]
        if interlace is None:
            interlace = ℤ[force_interlacings['gravity']['pm']]
        if differentiation_order == -1:
            differentiation_order = ℤ[force_differentiations['gravity']['pm']]
        particle_mesh(
            receivers, suppliers, quantity, φ_gridsizes_receivers, potential, potential_name,
            interpolation_order, interlace, differentiation_order, ᔑdt, ᔑdt_key,
        )
        if pm_potential == 'full':
            if printout:
                masterprint('done')
    elif method == 'pp':
        # The particle-particle method with Ewald-periodicity
        if printout:
            masterprint(
                'Executing',
                shortrange_progress_messages('gravity', method, receivers),
                '...',
            )
        get_ewald_grid()
        component_component(
            receivers, suppliers, gravity_pairwise, ᔑdt,
            dependent=['pos'],
            affected=['mom'],
            deterministic=True,
            pairing_level='domain',
            interaction_name='gravity',
        )
        if printout:
            masterprint('done')
    elif method == 'ppnonperiodic':
        # The non-periodic particle-particle method
        if printout:
            masterprint(
                'Executing',
                shortrange_progress_messages('gravity', method, receivers),
                '...',
            )
        component_component(
            receivers, suppliers, gravity_pairwise_nonperiodic, ᔑdt,
            dependent=['pos'],
            affected=['mom'],
            deterministic=True,
            pairing_level='domain',
            interaction_name='gravity',
        )
        if printout:
            masterprint('done')
    elif master:
        abort(f'gravity() was called with the "{method}" method')

# Function that carry out the lapse interaction,
# correcting for the fact that the decay rate of species should be
# measured with respect to their individual proper time.
@cython.pheader(
    # Arguments
    method=str,
    receivers=list,
    suppliers=list,
    ᔑdt=dict,
    interaction_type=str,
    printout='bint',
    # Locals
    interlace='bint',
    interpolation_order='int',
    differentiation_order='int',
    quantity=str,
    φ_gridsizes_receivers=list,
    ᔑdt_key=object,  # str or tuple
)
def lapse(method, receivers, suppliers, ᔑdt, interaction_type, printout):
    # While the receivers list stores the correct components,
    # the suppliers store the lapse component as well as all the
    # components also present as receivers. As the lapse force should be
    # supplied solely from the lapse component, we must remove these
    # additional components.
    suppliers = oneway_force(receivers, suppliers)
    if len(suppliers) == 0:
        abort('The lapse() function got no suppliers, but expected a lapse component.')
    elif len(suppliers) > 1:
        abort(
            f'The lapse() function got the following suppliers: {suppliers}, '
            f'but expected only a lapse component.'
        )
    # For the lapse force, only the PM method is implemented
    if method == 'pm':
        if printout:
            masterprint(
                f'Executing lapse interaction for {receivers[0].name} via the PM method ...'
                if len(receivers) == 1 else (
                    'Executing lapse interaction for {{{}}} via the PM method ...'
                    .format(', '.join([component.name for component in receivers]))
                )
            )
        # As the lapse potential is implemented exactly analogous to the
        # gravitational potential, it obeys the Poisson equation
        # ∇²φ = 4πGa²ρ = 4πGa**(-3*w_eff - 1)ϱ,
        # with φ the lapse potential and ρ, ϱ and w_eff belonging to the
        # fictitious lapse species.
        quantity = 'a²ρ'
        # As the lapse potential is implemented exactly analogous to the
        # gravitational potential, the momentum updates are again
        # proportional to a**(-3*w_eff) integrated over the time step
        # (see the gravity function for a more detailed explanation).
        # The realized lapse potential is the common lapse potential,
        # indepedent on the component in question which is to receive
        # momentum updates. The actual lapse potential needed for a
        # given component is obtained by multiplying the common lapse
        # potential by Γ/H, where Γ is the decay rate of the component
        # and H is the Hubble parameter. As these are time dependent,
        # the full time step integral is then a**(-3*w_eff)*Γ/H.
        ᔑdt_key = ('a**(-3*w_eff)*Γ/H', 'component')
        # Execute the lapse particle-mesh interaction.
        # As the lapse potential is exactly analogous to the
        # gravitational potential, we may reuse the gravity_potential
        # function implementing the Poisson equation for gravity.
        φ_gridsizes_receivers = [
            component.φ_gridsizes['lapse', 'pm'] for component in receivers
        ]
        interpolation_order   = ℤ[force_interpolations  ['lapse']['pm']]
        interlace             = ℤ[force_interlacings    ['lapse']['pm']]
        differentiation_order = ℤ[force_differentiations['lapse']['pm']]
        particle_mesh(
            receivers, suppliers, quantity, φ_gridsizes_receivers,
            gravity_potential, 'lapse potential',
            interpolation_order, interlace, differentiation_order, ᔑdt, ᔑdt_key,
        )
        if printout:
            masterprint('done')
    elif master:
        abort(f'lapse() was called with the "{method}" method')

# Function implementing progress messages used for the short-range
# kicks intertwined with drift operations.
@cython.pheader(
    # Arguments
    force=str,
    method=str,
    receivers=list,
    extra_message=str,
    # Locals
    component='Component',
    returns=str,
)
def shortrange_progress_messages(force, method, receivers, extra_message=' (short-range only)'):
    if force == 'gravity':
        if method == 'p3m':
            return (
                f'gravitational interaction for {receivers[0].name} via '
                f'the P³M method{extra_message}'
            ) if len(receivers) == 1 else (
                f'gravitational interaction for {{{{{{}}}}}} via the P³M method{extra_message}'
                .format(', '.join([component.name for component in receivers]))
            )
        elif method == 'pp':
            return (
                f'gravitational interaction for {receivers[0].name} via '
                f'the PP method'
            ) if len(receivers) == 1 else (
                'gravitational interaction for {{{}}} via the PP method'
                .format(', '.join([component.name for component in receivers]))
            )
        elif method == 'ppnonperiodic':
            return (
                f'gravitational interaction for {receivers[0].name} via '
                f'the non-periodic PP method'
            ) if len(receivers) == 1 else (
                'gravitational interaction for {{{}}} via the non-periodic PP method'
                .format(', '.join([component.name for component in receivers]))
            )
        else:
            abort(
                f'"{method}" is not a known method for '
                f'force "{force}" in shortrange_progress_messages()'
            )
    else:
        abort(f'Unknown force "{force}" supplied to shortrange_progress_messages()')

# Function that given lists of receiver and supplier components of a
# one-way interaction removes any components from the supplier list that
# are also present in the receiver list.
def oneway_force(receivers, suppliers):
    return [component for component in suppliers if component not in receivers]

# Function which constructs a list of interactions from a list of
# components. The list of interactions store information about which
# components interact with one another, via what force and method.
def find_interactions(components, interaction_type='any'):
    """You may specify an interaction_type to only get
    specific interactions. The options are:
    - interaction_type == 'any':
      Include every interaction.
    - interaction_type == 'long-range':
      Include long-range interactions only, i.e. ones with a method of
      either PM and P³M. Note that P³M interactions will also be
      returned for interaction_type == 'short-range'.
    - interaction_type == 'short-range':
      Include short-range interactions only, i.e. any other than PM.
      Note that P³M interactions will also be returned
      for interaction_type == 'short-range'.
    """
    # Use cached result
    interactions_list = interactions_lists.get(tuple(components + [interaction_type]))
    if interactions_list:
        return interactions_list
    # Find all (force, method) pairs in use. Store these as a (default)
    # dict mapping forces to lists of methods.
    forces_in_use = collections.defaultdict(set)
    for component in components:
        for force, method in component.forces.items():
            forces_in_use[force].add(method)
    # Check that all forces and methods assigned
    # to the components are implemented.
    for force, methods in forces_in_use.items():
        methods_implemented = forces_implemented.get(force, [])
        for method in methods:
            if not method:
                # When the method is set to an empty string it signifies
                # that this method should be used as a supplier for the
                # given force, but not receive the force itself.
                continue
            if method not in methods_implemented:
                abort(f'Method "{method}" for force "{force}" is not implemented')
    # Construct the interactions_list with (named) 4-tuples
    # in the format (force, method, receivers, suppliers),
    # where receivers is a list of all components which interact
    # via the force and should therefore receive momentum updates
    # computed via this force and the method given as the
    # second element. In the simple case where all components
    # interacting under some force using the same method, the suppliers
    # list holds the same components as the receivers list. When the
    # same force should be applied to several components using
    # different methods, the suppliers list still holds all components
    # as before, while the receivers list is limited to just those
    # components that should receive the force using the
    # specified method. Note that the receivers do not contribute to the
    # force unless they are also present in the suppliers list.
    interactions_list = []
    for force, methods in forces_implemented.items():
        for method in methods:
            if method not in forces_in_use.get(force, []):
                continue
            # Find all receiver and supplier components
            # for this (force, method) pair.
            receivers = []
            suppliers = []
            for component in components:
                if force in component.forces:
                    suppliers.append(component)
                    if component.forces[force] == method:
                        receivers.append(component)
            # Store the 4-tuple in the interactions_list
            interactions_list.append(Interaction(force, method, receivers, suppliers))
    # Cleanup the list of interactions
    def cleanup():
        nonlocal interactions_list
        # If fluid components are present as suppliers for interactions
        # using a method different from PM, remove them from the
        # suppliers list and create a new PM interaction instead.
        for i, interaction in enumerate(interactions_list):
            if interaction.method == 'pm':
                continue
            for component in interaction.suppliers:
                if component.representation == 'fluid':
                    interaction.suppliers.remove(component)
                    interactions_list.insert(
                        i + 1,
                        Interaction(
                            interaction.force, 'pm', interaction.receivers.copy(), [component],
                        )
                    )
                    return True
        # Remove interactions with no suppliers or no receivers
        interactions_list = [interaction for interaction in interactions_list
            if interaction.receivers and interaction.suppliers]
        # Merge interactions of identical force, method and receivers
        # but different suppliers, or identical force,
        # method and suppliers but different receivers.
        for     i, interaction_i in enumerate(interactions_list):
            for j, interaction_j in enumerate(interactions_list[i+1:], i+1):
                if interaction_i.force != interaction_j.force:
                    continue
                if interaction_i.method != interaction_j.method:
                    continue
                if (
                        set(interaction_i.receivers) == set(interaction_j.receivers)
                    and set(interaction_i.suppliers) != set(interaction_j.suppliers)
                ):
                    for supplier in interaction_j.suppliers:
                        if supplier not in interaction_i.suppliers:
                            interaction_i.suppliers.insert(0, supplier)
                    interactions_list.pop(j)
                    return True
                if (
                        set(interaction_i.receivers) != set(interaction_j.receivers)
                    and set(interaction_i.suppliers) == set(interaction_j.suppliers)
                ):
                    for receiver in interaction_j.receivers:
                        if receiver not in interaction_i.receivers:
                            interaction_i.receivers.insert(0, receiver)
                    interactions_list.pop(j)
                    return True
    while cleanup():
        pass
    # In the case that only some interactions should be considered,
    # remove the unwanted interactions.
    if 'long' in interaction_type:
        for interaction in interactions_list:
            if interaction.method not in {'pm', 'p3m'}:
                interaction.receivers[:] = []
        while cleanup():
            pass
    elif 'short' in interaction_type:
        for interaction in interactions_list:
            if interaction.method == 'pm':
                interaction.receivers[:] = []
        while cleanup():
            pass
    elif 'any' not in interaction_type:
        abort(f'find_interactions(): Unknown interaction_type "{interaction_type}"')
    # Cache the result and return it
    interactions_lists[tuple(components + [interaction_type])] = interactions_list
    return interactions_list
# Global dict of interaction lists populated by the above function
cython.declare(interactions_lists=dict)
interactions_lists = {}
# Create the Interaction type used in the above function
Interaction = collections.namedtuple(
    'Interaction', ('force', 'method', 'receivers', 'suppliers')
)

# Specification of implemented forces.
# The order specified here will be the order in which the forces
# are computed and applied.
# Importantly, all forces and methods should be written with purely
# alphanumeric, lowercase characters.
forces_implemented = {
    'gravity': ['ppnonperiodic', 'pp', 'p3m', 'pm'],
    'lapse'  : [                              'pm'],
}
