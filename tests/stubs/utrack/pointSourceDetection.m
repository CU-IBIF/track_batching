function pstruct = pointSourceDetection(img, sigma, varargin)
% Test stub
assert(mod(numel(varargin), 2) == 0);
for k = 1:2:numel(varargin)
    if strcmp(varargin{k}, 'FitMixtures'), assert(islogical(varargin{k + 1})); end
end
[~, idx] = max(img(:));
[y, x] = ind2sub(size(img), idx);
pstruct = struct('x', x, 'y', y, 'A', img(idx), 'x_pstd', 0.1, 'y_pstd', 0.1, 'A_pstd', 1);
end
