# Derivation for the app itself, kept separate from flake.nix so it can be
# lifted into pkgsnix unchanged: that repo callPackage's a default.nix per
# package, and this file already has that signature. Moving it there means
# copying this file to pkgsnix/microscope-control/default.nix and passing a
# `src` that is a fetchFromGitHub rather than the local tree.
{
  lib,
  python3Packages,
  qt6,
  src,
  version ? "0.1.0",
}:
python3Packages.buildPythonApplication {
  pname = "microscope-control";
  inherit src version;
  pyproject = true;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    pyside6
    numpy
    gphoto2 # python-gphoto2; pulls libgphoto2 itself
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];

  # wrapQtAppsHook reads qtPluginPrefix out of qtbase's setup hook and fails
  # outright without it. pyside6 brings its own copy of the Qt libraries, so
  # this is here for the hook's benefit rather than to link against.
  buildInputs = [ qt6.qtbase ];

  # buildPythonApplication makes its own wrapper, so wrapQtAppsHook's would be
  # discarded. Folding qtWrapperArgs into makeWrapperArgs is the nixpkgs
  # pattern for Qt apps built by a Python builder -- without it the app starts
  # with no QPA platform plugin and dies looking for xcb/wayland.
  preFixup = ''
    makeWrapperArgs+=("''${qtWrapperArgs[@]}")
  '';

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # Only the focus metrics are covered, and that is on purpose: they are pure
  # numpy, so they run without a display, and they are the part that fails
  # *quietly* -- a broken metric still shows a plausible number. The Qt layer
  # fails loudly enough that a smoke run catches it.
  enabledTestPaths = [ "tests/" ];
  pythonImportsCheck = [ "microscope_control" ];

  meta = {
    description = "Live view, focus aids and capture for a tethered Canon EOS on an Olympus BH3";
    platforms = lib.platforms.linux;
    mainProgram = "microscope-control";
  };
}
