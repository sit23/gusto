
from firedrake import (interval, FiniteElement, TensorProductElement, FunctionSpace,
                       VectorFunctionSpace)
from gusto.core.function_spaces import DeRhamComplex
from gusto.core.configuration import RecoveryOptions


class RecoverySpaces(object):
    """
    Finds or builds necessary spaces to carry out recovery transport for lowest
    and mixed order domains (0,0), (0,1) and  (1,0)
    """

    def __init__(self, domain, boundary_method=None, use_vector_spaces=False):
        """
        Args:
            domain (:class:`Domain`): the model's domain object, containing the
            mesh and the compatible function spaces.

            boundary_method (:variable:'dict', optional: A dictionary containing the space
            the boundary method is to be applied to along with specified method. Acceptable keys are "DG",
            "HDiv" and theta, defaults to None

            use_vector_spaces (boolean, optional) Determins if we need to use DG / CG
            space for the embedded and recovery space for the HDiv field instead of the usual
            HDiv, Hcurs spaces. Defaults to False
        """
        family = domain.family
        mesh = domain.mesh
        # Need spaces from current deRham and a higher order deRham
        self.de_Rham = DeRhamComplex(mesh, family,
                                     horizontal_degree=1,
                                     vertical_degree=1,
                                     complex_name='recovery_de_Rham')
        
        valid_keys = ['DG', 'HDiv', 'theta']
        if boundary_method is not None:
                for key in boundary_method:
                    if key not in valid_keys:
                        raise KeyError(f'Recovery spaces: boundary method key {key} not valid. Valid keys are DG, HDiv, theta')

        # ----------------------------------------------------------------------
        # Building theta options if on an extruded mesh
        # ----------------------------------------------------------------------

        # Check if extruded and if so builds theta spaces
        if hasattr(mesh, "_base_mesh"):
            valid_theta_keys = ['theta']
            theta_boundary_method = get_first_valid_value(boundary_method, valid_theta_keys)
            cell = mesh._base_mesh.ufl_cell().cellname()
            DG_hori_ele = FiniteElement('DG', cell, 1, variant='equispaced')
            DG_vert_ele = FiniteElement('DG', interval, (domain.vertical_degree + 1), variant='equispaced')
            CG_hori_ele = FiniteElement('CG', cell, 1)
            CG_vert_ele = FiniteElement('CG', interval, (domain.vertical_degree + 1))

            VDG_ele = TensorProductElement(DG_hori_ele, DG_vert_ele)
            VCG_ele = TensorProductElement(CG_hori_ele, CG_vert_ele)
            VDG_theta = FunctionSpace(mesh, VDG_ele)
            VCG_theta = FunctionSpace(mesh, VCG_ele)

            self.theta_options = RecoveryOptions(embedding_space=VDG_theta,
                                                 recovered_space=VCG_theta,
                                                 boundary_method=theta_boundary_method)
        else:
            cell = self.mesh.ufl_cell().cellname()

        # ----------------------------------------------------------------------
        # Building the DG options
        # ----------------------------------------------------------------------
        valid_DG_keys = ['DG']
        DG_boundary_method = get_first_valid_value(boundary_method, valid_DG_keys)

        DG_embedding_space = domain.spaces.DG1_equispaced
        # Recovered space needs builing manually to avoid uneccesary DoFs
        CG_hori_ele_DG = FiniteElement('CG', cell, 1)
        CG_vert_ele_DG = FiniteElement('CG', interval, 1)
        VCG_ele_DG = TensorProductElement(CG_hori_ele_DG, CG_vert_ele_DG)
        DG_recovered_space = FunctionSpace(mesh, VCG_ele_DG)

        # DG_recovered_space = domain.spaces.H1
        self.DG_options = RecoveryOptions(embedding_space=DG_embedding_space,
                                          recovered_space=DG_recovered_space,
                                          boundary_method=DG_boundary_method)
        # ----------------------------------------------------------------------
        # Building HDiv options
        # ----------------------------------------------------------------------
        valid_HDiv_keys = ['HDiv']
        HDiv_boundary_method = get_first_valid_value(boundary_method, valid_HDiv_keys)

        if use_vector_spaces:
            Vu_DG1 = VectorFunctionSpace(mesh, DG_embedding_space.ufl_element())
            Vu_CG1 = VectorFunctionSpace(mesh, "CG", 1)

            HDiv_embedding_Space = Vu_DG1
            HDiv_recovered_Space = Vu_CG1

        else:

            HDiv_embedding_Space = self.de_Rham.HDiv
            HDiv_recovered_Space = self.de_Rham.HCurl

        self.HDiv_options = RecoveryOptions(embedding_space=HDiv_embedding_Space,
                                            recovered_space=HDiv_recovered_Space,
                                            injection_method='recover',
                                            project_high_method='project',
                                            project_low_method='project',
                                            broken_method='project',
                                            boundary_method=HDiv_boundary_method)
