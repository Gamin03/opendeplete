""" The OpenMC wrapper module.

This module implements the OpenDeplete -> OpenMC linkage.
"""

import copy
from collections import OrderedDict
import os
import random
from subprocess import call
import sys
import time
try:
    import lxml.etree as ET
    _have_lxml = True
except ImportError:
    import xml.etree.ElementTree as ET
    from openmc.clean_xml import clean_xml_indentation
    _have_lxml = False

import h5py
from mpi4py import MPI
import numpy as np
import openmc
from openmc.stats import Box

from .atom_number import AtomNumber
from .depletion_chain import DepletionChain
from .reaction_rates import ReactionRates
from .function import Settings, Operator

class OpenMCSettings(Settings):
    """ The OpenMCSettings class.

    Extends Settings to provide information OpenMC needs to run.

    Attributes
    ----------
    dt_vec : numpy.array
        Array of time steps to take. (From Settings)
    tol : float
        Tolerance for adaptive time stepping. (From Settings)
    output_dir : str
        Path to output directory to save results. (From Settings)
    chain_file : str
        Path to the depletion chain xml file.  Defaults to the environment
        variable "OPENDEPLETE_CHAIN" if it exists.
    openmc_call : list of str
        The command to be used with subprocess.call to run a simulation. If no
        arguments are to be passed, a string suffices.  To run with mpiexec,
        a list of strings is needed.
    particles : int
        Number of particles to simulate per batch.
    batches : int
        Number of batches.
    inactive : int
        Number of inactive batches.
    lower_left : list of float
        Coordinate of lower left of bounding box of geometry.
    upper_right : list of float
        Coordinate of upper right of bounding box of geometry.
    entropy_dimension : list of int
        Grid size of entropy.
    round_number : bool
        Whether or not to round output to OpenMC to 8 digits.
        Useful in testing, as OpenMC is incredibly sensitive to exact values.
    constant_seed : int
        If present, all runs will be performed with this seed.
    power : float
        Power of the reactor (currently in MeV/second-cm).
    """

    def __init__(self):
        Settings.__init__(self)
        # OpenMC specific
        try:
            self.chain_file = os.environ["OPENDEPLETE_CHAIN"]
        except KeyError:
            self.chain_file = None
        self.openmc_call = None
        self.particles = None
        self.batches = None
        self.inactive = None
        self.lower_left = None
        self.upper_right = None
        self.entropy_dimension = None

        # OpenMC testing specific
        self.round_number = False
        self.constant_seed = None

        # Depletion problem specific
        self.power = None

class Materials(object):
    """The Materials class.

    Contains information about cross sections for a cell.

    Attributes
    ----------
    temperature : float
        Temperature in Kelvin for each region.
    sab : str or list of str
        ENDF S(a,b) name for a region that needs S(a,b) data.  Not set if no
        S(a,b) needed for region.
    """

    def __init__(self):
        self.temperature = None
        self.sab = None


class OpenMCOperator(Operator):
    """The OpenMC Operator class.

    Provides Operator functions for OpenMC.

    Parameters
    ----------
    geometry : openmc.Geometry
        The OpenMC geometry object.
    settings : OpenMCSettings
        Settings object.

    Attributes
    ----------
    settings : OpenMCSettings
        Settings object. (From Operator)
    geometry : openmc.Geometry
        The OpenMC geometry object.
    materials : list of Materials
        Materials to be used for this simulation.
    seed : int
        The RNG seed used in last OpenMC run.
    number : AtomNumber
        Total number of atoms in simulation.
    participating_nuclides : set of str
        A set listing all unique nuclides available from cross_sections.xml.
    chain : DepletionChain
        The depletion chain information necessary to form matrices and tallies.
    reaction_rates : ReactionRates
        Reaction rates from the last operator step.
    power : OrderedDict of str to float
        Material-by-Material power.  Indexed by material ID.
    mat_name : OrderedDict of str to int
        The name of region each material is set to.  Indexed by material ID.
    burn_mat_to_id : OrderedDict of str to int
        Dictionary mapping material ID (as a string) to an index in reaction_rates.
    burn_nuc_to_id : OrderedDict of str to int
        Dictionary mapping nuclide name (as a string) to an index in
        reaction_rates.
    n_nuc : int
        Number of nuclides considered in the decay chain.
    comm : MPI.COMM_WORLD
        The mpi communicator.
    rank : int
        MPI rank of this object.
    size : int
        The number of MPI threads.
    mat_tally_ind : OrderedDict of str to int
        Dictionary mapping material ID to index in tally.
    """

    def __init__(self, geometry, settings):
        Operator.__init__(self, settings)

        self.comm = MPI.COMM_WORLD

        self.rank = self.comm.rank
        self.size = self.comm.size

        self.geometry = geometry
        self.materials = []
        self.seed = 0
        self.number = None
        self.participating_nuclides = None
        self.reaction_rates = None
        self.power = None
        self.mat_name = OrderedDict()
        self.burn_mat_to_ind = OrderedDict()
        self.burn_nuc_to_ind = None

        # Read depletion chain
        self.chain = DepletionChain()
        self.chain.xml_read(settings.chain_file)

        # Clear out OpenMC, create task lists, distribute
        if self.rank == 0:
            clean_up_openmc()
            mat_burn_lists, \
                mat_not_burn_lists, \
                volume, \
                self.mat_tally_ind, \
                nuc_dict = self.extract_mat_ids()

            mat_burn = mat_burn_lists[0]
            mat_not_burn = mat_not_burn_lists[0]

            # Send assignments to all
            for i in range(1, self.size):
                self.comm.send(mat_burn_lists[i], dest=i, tag=0)
                self.comm.send(mat_not_burn_lists[i], dest=i, tag=1)
                self.comm.send(nuc_dict, dest=i, tag=2)
        else:
            # Receive assignments
            mat_burn = self.comm.recv(source=0, tag=0)
            mat_not_burn = self.comm.recv(source=0, tag=1)
            nuc_dict = self.comm.recv(source=0, tag=2)
            volume = None
            self.mat_tally_ind = None

        volume = self.comm.bcast(volume, root=0)
        self.mat_tally_ind = self.comm.bcast(self.mat_tally_ind, root=0)

        # Extract number densities from the geometry
        self.extract_number(mat_burn, mat_not_burn, volume, nuc_dict)

        # Load participating nuclides
        self.load_participating()

        # Create reaction rate tables
        self.initialize_reaction_rates()

    def extract_mat_ids(self):
        """ Extracts materials and assigns them to processes.

        Returns
        -------
        mat_burn_lists : list of list of int
            List of burnable materials indexed by rank.
        mat_not_burn_lists : list of list of int
            List of non-burnable materials indexed by rank.
        volume : OrderedDict of str to float
            Volume of each cell
        mat_tally_ind : OrderedDict of str to int
            Dictionary mapping material ID to index in tally.
        nuc_dict : OrderedDict of str to int
            Nuclides in order of how they'll appear in the simulation.
        """

        mat_burn = set()
        mat_not_burn = set()
        nuc_set = set()

        volume = OrderedDict()

        # Iterate once through the geometry to get dictionaries
        cells = self.geometry.get_all_material_cells()
        for cell_id in cells:
            cell = cells[cell_id]
            name = cell.name

            if isinstance(cell.fill, openmc.Material):
                mat = cell.fill
                for nuclide in mat.nuclides:
                    nuc_set.add(nuclide[0].name)
                if mat.burnable:
                    mat_burn.add(str(mat.id))
                    volume[str(mat.id)] = mat.volume
                else:
                    mat_not_burn.add(str(mat.id))
                self.mat_name[mat.id] = name
            else:
                for mat in cell.fill:
                    for nuclide in mat.nuclides:
                        nuc_set.add(nuclide[0].name)
                    if mat.burnable:
                        mat_burn.add(str(mat.id))
                        volume[str(mat.id)] = mat.volume
                    else:
                        mat_not_burn.add(str(mat.id))
                    self.mat_name[mat.id] = name

        need_vol = []

        for mat_id in volume:
            if volume[mat_id] is None:
                need_vol.append(mat_id)

        if need_vol:
            exit("Need volumes for materials: " + str(need_vol))

        # Alphabetize the sets
        mat_burn = sorted(list(mat_burn))
        mat_not_burn = sorted(list(mat_not_burn))
        nuc_set = sorted(list(nuc_set))

        # Construct a global nuclide dictionary, burned first
        nuc_dict = copy.copy(self.chain.nuclide_dict)

        i = len(nuc_dict)

        for nuc in nuc_set:
            if nuc not in nuc_dict:
                nuc_dict[nuc] = i
                i += 1

        # Decompose geometry
        n = self.size
        chunk, extra = divmod(len(mat_burn), n)
        mat_burn_lists = []
        j = 0
        for i in range(n):
            if i < extra:
                c_chunk = chunk + 1
            else:
                c_chunk = chunk
            mat_burn_chunk = mat_burn[j:j + c_chunk]
            j += c_chunk
            mat_burn_lists.append(mat_burn_chunk)

        chunk, extra = divmod(len(mat_not_burn), n)
        mat_not_burn_lists = []
        j = 0
        for i in range(n):
            if i < extra:
                c_chunk = chunk + 1
            else:
                c_chunk = chunk
            mat_not_burn_chunk = mat_not_burn[j:j + c_chunk]
            j += c_chunk
            mat_not_burn_lists.append(mat_not_burn_chunk)

        mat_tally_ind = OrderedDict()

        for i, mat in enumerate(mat_burn):
            mat_tally_ind[mat] = i

        return mat_burn_lists, mat_not_burn_lists, volume, mat_tally_ind, nuc_dict

    def extract_number(self, mat_burn, mat_not_burn, volume, nuc_dict):
        """ Construct self.number read from geometry

        Parameters
        ----------
        mat_burn : list of int
            Materials to be burned managed by this thread.
        mat_not_burn
            Materials not to be burned managed by this thread.
        volume : OrderedDict of str to float
            Volumes for the above materials.
        nuc_dict : OrderedDict of str to int
            Nuclides to be used in the simulation.
        """

        # Same with materials
        mat_dict = OrderedDict()
        self.burn_mat_to_ind = OrderedDict()
        i = 0
        for mat in mat_burn:
            mat_dict[mat] = i
            self.burn_mat_to_ind[mat] = i
            i += 1

        for mat in mat_not_burn:
            mat_dict[mat] = i
            i += 1

        n_mat_burn = len(mat_burn)
        n_nuc_burn = len(self.chain.nuclide_dict)

        self.number = AtomNumber(mat_dict, nuc_dict, volume, n_mat_burn, n_nuc_burn)

        self.materials = [None] * self.number.n_mat

        # Now extract the number densities and store
        cells = self.geometry.get_all_material_cells()
        for cell_id in cells:
            cell = cells[cell_id]
            if isinstance(cell.fill, openmc.Material):
                if str(cell.fill.id) in mat_dict:
                    self.set_number_from_mat(cell.fill)
            else:
                for mat in cell.fill:
                    if str(mat.id) in mat_dict:
                        self.set_number_from_mat(mat)

    def set_number_from_mat(self, mat):
        """ Extracts material and number densities from openmc.Material

        Parameters
        ----------
        mat : openmc.Materials
            The material to read from
        """

        mat_id = str(mat.id)
        mat_ind = self.number.mat_to_ind[mat_id]

        self.materials[mat_ind] = Materials()
        self.materials[mat_ind].sab = mat._sab
        self.materials[mat_ind].temperature = mat.temperature

        for nuclide in mat.nuclides:
            name = nuclide[0].name
            number = nuclide[1] * 1.0e24
            self.number.set_atom_density(mat_id, name, number)

    def initialize_reaction_rates(self):
        """ Create reaction rates object. """
        self.reaction_rates = ReactionRates(
            self.burn_mat_to_ind,
            self.burn_nuc_to_ind,
            self.chain.react_to_ind)

        self.chain.nuc_to_react_ind = self.burn_nuc_to_ind

    def eval(self, vec, print_out=True):
        """ Runs a simulation.

        Parameters
        ----------
        vec : list of numpy.array
            Total atoms to be used in function.
        print_out : bool, optional
            Whether or not to print out time.

        Returns
        -------
        mat : list of scipy.sparse.csr_matrix
            Matrices for the next step.
        k : float
            Eigenvalue of the problem.
        rates : ReactionRates
            Reaction rates from this simulation.
        seed : int
            Seed for this simulation.
        """

        # Update status
        self.set_density(vec)

        # Recreate model
        self.generate_materials_xml()
        self.generate_tally_xml()
        self.generate_settings_xml()

        if self.rank == 0:
            time_start = time.time()

            # Run model
            call(self.settings.openmc_call)
            time_openmc = time.time()

            for i in range(1, self.size):
                self.comm.send(True, dest=i, tag=0)

        # We don't want to use a barrier here, as that will slow down OpenMC
        # due to spinlocking. Instead, we'll do async send-receives
        if self.rank != 0:
            status = self.comm.irecv(source=0, tag=0)
            while not status.Test():
                time.sleep(1)

        statepoint_name = "statepoint." + str(self.settings.batches) + ".h5"

        # Extract results
        k = self.unpack_tallies_and_normalize(statepoint_name)

        self.comm.barrier()

        if self.rank == 0:
            time_unpack = time.time()
            os.remove(statepoint_name)

            if print_out:
                print("Time to openmc: ", time_openmc - time_start)
                print("Time to unpack: ", time_unpack - time_openmc)

        return k, self.reaction_rates, self.seed

    def form_matrix(self, y, mat):
        """ Forms the depletion matrix.

        Parameters
        ----------
        y : numpy.ndarray
            An array representing reaction rates for this cell.
        mat : int
            Material id.

        Returns
        -------
        scipy.sparse.csr_matrix
            Sparse matrix representing the depletion matrix.
        """

        return copy.deepcopy(self.chain.form_matrix(y[mat, :, :]))

    def initial_condition(self):
        """ Performs final setup and returns initial condition.

        Returns
        -------
        list of numpy.array
            Total density for initial conditions.
        """

        # Write geometry.xml
        if self.rank == 0:
            self.geometry.export_to_xml()

        # Return number density vector
        return self.total_density_list()

    def generate_materials_xml(self):
        """ Creates materials.xml from self.number.

        Due to uncertainty with how MPI interacts with OpenMC API, this
        constructs the XML manually.  The long term goal is to do this
        either through PHDF5 or direct memory writing.
        """

        xml_strings = []

        for mat in self.number.mat_to_ind:
            root = ET.Element("material")
            root.set("id", mat)

            density = ET.SubElement(root, "density")
            density.set("units", "sum")

            temperature = ET.SubElement(root, "temperature")
            mat_id = self.number.mat_to_ind[mat]
            temperature.text = str(self.materials[mat_id].temperature)

            for nuc in self.number.nuc_to_ind:
                if nuc in self.participating_nuclides:
                    val = 1.0e-24*self.number.get_atom_density(mat, nuc)

                    if val > 0.0:
                        if self.settings.round_number:
                            val_magnitude = np.floor(np.log10(val))
                            val_scaled = val / 10**val_magnitude
                            val_round = round(val_scaled, 8)

                            val = val_round * 10**val_magnitude

                        nuc_element = ET.SubElement(root, "nuclide")
                        nuc_element.set("ao", str(val))
                        nuc_element.set("name", nuc)
                    else:
                        # Only output warnings if values are significantly
                        # negative.  CRAM does not guarantee positive values.
                        if val < -1.0e-21:
                            print("WARNING: nuclide ", nuc, " in material ", mat,
                                  " is negative (density = ", val, " at/barn-cm)")
                        self.number[mat, nuc] = 0.0

            for sab in self.materials[mat_id].sab:
                sab_el = ET.SubElement(root, "sab")
                sab_el.set("name", sab)

            if _have_lxml:
                fragment = ET.tostring(root, encoding="unicode", pretty_print="true")
                xml_strings.append(fragment)
            else:
                clean_xml_indentation(root, spaces_per_level=2)
                fragment = ET.tostring(root, encoding="unicode", pretty_print="true")
                xml_strings.append(fragment)

        xml_string = "".join(xml_strings)

        # Communicate xml_string to rank=0, which will stream to disk.
        # This means only 2 xml strings will exist on any node.
        if self.rank == 0:
            f = open("materials.xml", mode="w")

            # Fill with header
            f.write("<?xml version='1.0' encoding='utf-8'?>\n<materials>\n")
            f.write(xml_string)

            for i in range(1, self.size):
                xml_string = self.comm.recv(source=i, tag=i)
                f.write(xml_string)

            f.write("\n</materials>")
            f.close()
        else:
            self.comm.send(xml_string, dest=0, tag=self.rank)

        self.comm.barrier()

    def generate_settings_xml(self):
        """ Generates settings.xml.

        This function creates settings.xml using the value of the settings
        variable.

        Todo
        ----
            Rewrite to generalize source box.
        """

        if self.rank == 0:
            batches = self.settings.batches
            inactive = self.settings.inactive
            particles = self.settings.particles

            # Just a generic settings file to get it running.
            settings_file = openmc.Settings()
            settings_file.batches = batches
            settings_file.inactive = inactive
            settings_file.particles = particles
            settings_file.source = openmc.Source(space=Box(self.settings.lower_left,
                                                           self.settings.upper_right))
            settings_file.entropy_lower_left = self.settings.lower_left
            settings_file.entropy_upper_right = self.settings.upper_right
            settings_file.entropy_dimension = self.settings.entropy_dimension

            # Set seed
            if self.settings.constant_seed is not None:
                seed = self.settings.constant_seed
            else:
                seed = random.randint(1, sys.maxsize-1)

            self.seed = seed
            settings_file.seed = seed

            settings_file.export_to_xml()

    def generate_tally_xml(self):
        """ Generates tally.xml.

        Using information from self.depletion_chain as well as the nuclides
        currently in the problem, this function automatically generates a
        tally.xml for the simulation.
        """

        nuc_set = set()

        # Create the set of all nuclides in the decay chain in cells marked for
        # burning in which the number density is greater than zero.
        for nuc in self.number.nuc_to_ind:
            if nuc in self.participating_nuclides:
                if np.sum(self.number[:, nuc]) > 0.0:
                    nuc_set.add(nuc)

        # Communicate which nuclides have nonzeros to rank 0
        if self.rank == 0:
            for i in range(1, self.size):
                nuc_newset = self.comm.recv(source=i, tag=i)
                for nuc in nuc_newset:
                    nuc_set.add(nuc)

            # Sort them in the same order as self.number
            nuc_list = []
            for nuc in self.number.nuc_to_ind:
                if nuc in nuc_set:
                    nuc_list.append(nuc)
        else:
            self.comm.send(nuc_set, dest=0, tag=self.rank)

        if self.rank == 0:
            # Create tallies for depleting regions
            tally_ind = 1
            mat_filter_dep = openmc.MaterialFilter([int(id) for id in self.mat_tally_ind])
            tallies_file = openmc.Tallies()

            # For each reaction in the chain, for each nuclide, and for each
            # cell, make a tally
            tally_dep = openmc.Tally(tally_id=tally_ind)
            for key in nuc_list:
                if key in self.chain.nuclide_dict:
                    tally_dep.nuclides.append(key)

            for reaction in self.chain.react_to_ind:
                tally_dep.scores.append(reaction)

            tallies_file.append(tally_dep)

            tally_dep.filters.append(mat_filter_dep)
            tallies_file.export_to_xml()

    def total_density_list(self):
        """ Returns a list of total density lists.

        This list is in the exact same order as depletion_matrix_list, so that
        matrix exponentiation can be done easily.

        Returns
        -------
        list of numpy.array
            A list of np.arrays containing total atoms of each cell.
        """

        total_density = [self.number.get_mat_slice(i) for i in range(self.number.n_mat_burn)]

        return total_density

    def set_density(self, total_density):
        """ Sets density.

        Sets the density in the exact same order as total_density_list outputs,
        allowing for internal consistency

        Parameters
        ----------
        total_density : list of numpy.array
            Total atoms.
        """

        # Fill in values
        for i in range(self.number.n_mat_burn):
            self.number.set_mat_slice(i, total_density[i])

    def unpack_tallies_and_normalize(self, filename):
        """ Unpack tallies from OpenMC

        This function reads the tallies generated by OpenMC (from the tally.xml
        file generated in generate_tally_xml) normalizes them so that the total
        power generated is new_power, and then stores them in the reaction rate
        database.

        Parameters
        ----------
        filename : str
            The statepoint file to read from.

        Returns
        -------
        k : float
            Eigenvalue of the last simulation.

        Todo
        ----
            Provide units for power
        """

        self.reaction_rates[:, :, :] = 0.0

        file = h5py.File(filename, "r", driver='mpio', comm=self.comm)

        k_combined = file["k_combined"][0]

        # Extract tally bins
        materials_int = file["tallies/tally 1/filter 1/bins"].value
        materials = [str(mat) for mat in materials_int]

        nuclides_binary = file["tallies/tally 1/nuclides"].value
        nuclides = [nuc.decode('utf8') for nuc in nuclides_binary]

        reactions_binary = file["tallies/tally 1/score_bins"].value
        reactions = [react.decode('utf8') for react in reactions_binary]

        # Form fast map
        nuc_ind = [self.reaction_rates.nuc_to_ind[nuc] for nuc in nuclides]
        react_ind = [self.reaction_rates.react_to_ind[react] for react in reactions]

        # Compute fission power
        # TODO : improve this calculation

        power = 0.0

        power_vec = np.zeros(self.reaction_rates.n_nuc)

        fission_ind = self.reaction_rates.react_to_ind["fission"]

        for nuclide in self.chain.nuclides:
            if nuclide.name in self.reaction_rates.nuc_to_ind:
                ind = self.reaction_rates.nuc_to_ind[nuclide.name]

                power_vec[ind] = nuclide.fission_power

        # Extract results
        for i, mat in enumerate(self.number.burn_mat_list):
            # Get tally index
            slab = materials.index(mat)

            # Get material results hyperslab
            results = file["tallies/tally 1/results"][slab, :, 0]

            results_expanded = np.zeros((self.reaction_rates.n_nuc, self.reaction_rates.n_react))
            number = np.zeros((self.reaction_rates.n_nuc))

            # Expand into our memory layout
            j = 0
            for i_nuc_array, i_nuc_results in enumerate(nuc_ind):
                nuc = nuclides[i_nuc_array]
                for react in react_ind:
                    results_expanded[i_nuc_results, react] = results[j]
                    number[i_nuc_results] = self.number[mat, nuc]
                    j += 1

            # Add power
            power += np.dot(results_expanded[:, fission_ind], power_vec)

            # Divide by total number and store
            for i_nuc_results in nuc_ind:
                for react in react_ind:
                    if number[i_nuc_results] != 0.0:
                        results_expanded[i_nuc_results, react] /= number[i_nuc_results]

            self.reaction_rates.rates[i, :, :] = results_expanded

        # Communicate xml_string to rank=0, which will stream to disk.
        # This means only 2 xml strings will exist on any node.
        if self.rank == 0:
            power_array = np.zeros(self.size)
            power_array[0] = power

            for i in range(1, self.size):
                power_array[i] = self.comm.recv(source=i, tag=i)

            power = np.sum(power_array)
        else:
            self.comm.send(power, dest=0, tag=self.rank)

        power = self.comm.bcast(power, root=0)

        self.reaction_rates[:, :, :] *= (self.settings.power / power)

        return k_combined

    def load_participating(self):
        """ Loads a cross_sections.xml file to find participating nuclides.

        This allows for nuclides that are important in the decay chain but not
        important neutronically, or have no cross section data.
        """

        # Reads cross_sections.xml to create a dictionary containing
        # participating (burning and not just decaying) nuclides.

        try:
            filename = os.environ["OPENMC_CROSS_SECTIONS"]
        except KeyError:
            filename = None

        self.participating_nuclides = set()

        try:
            tree = ET.parse(filename)
        except:
            if filename is None:
                print("No cross_sections.xml specified in materials.")
            else:
                print('Cross section file "', filename, '" is invalid.')
            raise

        root = tree.getroot()
        self.burn_nuc_to_ind = OrderedDict()
        nuc_ind = 0

        for nuclide_node in root.findall('library'):
            mats = nuclide_node.get('materials')
            if not mats:
                continue
            for name in mats.split():
                # Make a burn list of the union of nuclides in cross_sections.xml
                # and nuclides in depletion chain.
                if name not in self.participating_nuclides:
                    self.participating_nuclides.add(name)
                    if name in self.chain.nuclide_dict:
                        self.burn_nuc_to_ind[name] = nuc_ind
                        nuc_ind += 1

    @property
    def n_nuc(self):
        """Number of nuclides considered in the decay chain."""
        return len(self.chain.nuclides)

    def get_results_info(self):
        """ Returns volume list, cell lists, and nuc lists.

        Returns
        -------
        volume : dict of str float
            Volumes corresponding to materials in full_burn_dict
        nuc_list : list of str
            A list of all nuclide names. Used for sorting the simulation.
        burn_list : list of int
            A list of all cell IDs to be burned.  Used for sorting the simulation.
        full_burn_dict : OrderedDict of str to int
            Maps cell name to index in global geometry.
        """

        nuc_list = self.number.burn_nuc_list
        burn_list = self.number.burn_mat_list

        volume = {}
        for i, mat in enumerate(burn_list):
            volume[mat] = self.number.volume[i]

        if self.rank == 0:
            for i in range(1, self.size):
                volume_new = self.comm.recv(source=i, tag=i)
                for mat, vol in volume_new.items():
                    volume[mat] = vol
        else:
            self.comm.send(volume, dest=0, tag=self.rank)

        volume = self.comm.bcast(volume, root=0)

        return volume, nuc_list, burn_list, self.mat_tally_ind

def density_to_mat(dens_dict):
    """ Generates an OpenMC material from a cell ID and self.number_density.
    Parameters
    ----------
    m_id : int
        Cell ID.
    Returns
    -------
    openmc.Material
        The OpenMC material filled with nuclides.
    """

    mat = openmc.Material()
    for key in dens_dict:
        mat.add_nuclide(key, 1.0e-24*dens_dict[key])
    mat.set_density('sum')

    return mat

def clean_up_openmc():
    """ Resets all automatic indexing in OpenMC, as these get in the way. """
    openmc.reset_auto_material_id()
    openmc.reset_auto_surface_id()
    openmc.reset_auto_cell_id()
    openmc.reset_auto_universe_id()
